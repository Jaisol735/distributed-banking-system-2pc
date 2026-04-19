"""
jpmc.py  —  JPMC Bank Server  (port 6001, USA)
================================================
Responsibilities:
  • Signup / Login  (bcrypt PIN)
  • Card generation: XXXX6001XXXX
  • Balance check   (PIN-gated)
  • Deposit         (FIFO queue → COMMITTED atomically)
  • Withdraw        (FIFO queue → COMMITTED atomically)
  • Transfer        (full 2PC: INIT → PREPARE → COMMIT / ABORT)
  • Receiver-side 2PC endpoints: /receive_prepare, /receive_commit, /receive_abort
  • Transaction history
  • Card lookup     (for transfer UI)

Two-Phase Commit rules
  Same bank   : both accounts in one DB transaction — no external coordination
  Same country: country server gives routing ACK; sender debits and calls
                receiver's /receive_prepare then /receive_commit
  Cross-country: country → world validates; same 2PC flow between banks

FIFO guarantee
  Deposits and withdrawals go through tx_requests table.
  Worker uses FOR UPDATE SKIP LOCKED → deadlock-free ordered processing.

Recovery
  Run sbi_recovery.py before accepting traffic.
  Any PREPARED sender transaction is refunded (ABORT + compensate).
  Stuck PROCESSING queue rows are reset to PENDING.
"""

import sys
import os
import json
import time
import uuid
import random
import string
import threading
import psycopg2
import requests
import subprocess
import bcrypt

from flask import Flask, request, jsonify
from datetime import datetime

# Add shared config tools
_this_dir = os.path.dirname(os.path.abspath(__file__))
CONFIG_DIR = os.path.abspath(os.path.join(_this_dir, "..", "..", ".."))  # /Server
if not os.path.exists(os.path.join(CONFIG_DIR, "config_loader.py")):
    CONFIG_DIR = os.path.abspath(os.path.join(_this_dir, "..", ".."))  # /Server/India or /Server/USA
if not os.path.exists(os.path.join(CONFIG_DIR, "config_loader.py")):
    raise FileNotFoundError("config_loader.py not found")
sys.path.insert(0, CONFIG_DIR)
from config_loader import server_urls, PORTS, TAILSCALE_IP

# ──────────────────────────────────────────────
# CONFIG
# ──────────────────────────────────────────────

BANK_NAME = "JPMC"
BANK_PORT = PORTS["JPMC"]

COUNTRY_URLS = server_urls(PORTS.get("usa") or PORTS.get("USA"))

# Port → bank mapping (used to detect receiver bank from card number)
PORT_BANK_MAP = {
    "5001": "SBI",
    "5002": "HDFC",
    "6001": "JPMC",
    "6002": "BOA",
}
BANK_PORT_MAP = {v: int(k) for k, v in PORT_BANK_MAP.items()}

LOG_FILE = "bank_log.json"
APP      = Flask(__name__)

# ──────────────────────────────────────────────
# UTILITIES
# ──────────────────────────────────────────────

def ts():
    return datetime.now().strftime("%H:%M:%S.%f")[:-3]

def log(msg):
    print(f"[{BANK_NAME}][{ts()}] {msg}")

def load_cfg():
    with open(LOG_FILE) as f:
        return json.load(f)

def connect_db():
    cfg = load_cfg()["supabase"]
    return psycopg2.connect(
        host=cfg["host"], port=cfg["port"],
        database=cfg["database"], user=cfg["user"],
        password=cfg["password"], connect_timeout=10,
    )

def audit(cur, tx_id, message):
    try:
        cur.execute(
            "INSERT INTO audit_log(tx_id, message) VALUES(%s, %s)",
            (tx_id, message),
        )
    except Exception:
        pass

def detect_bank(card: str):
    """Return bank name from card's middle 4 digits, or None."""
    if len(card) == 12:
        return PORT_BANK_MAP.get(card[4:8])
    return None

def country_post(path, payload=None, timeout=5):
    last_err = None
    for url in COUNTRY_URLS:
        try:
            r = requests.post(f"{url}{path}", json=payload, timeout=timeout)
            return r
        except Exception as e:
            last_err = e
    raise ConnectionError(f"Country servers unreachable: {last_err}")

def bank_post(bank, path, payload=None, timeout=8):
    port = BANK_PORT_MAP[bank]
    for base in server_urls(port):
        try:
            r = requests.post(f"{base}{path}", json=payload, timeout=timeout)
            return r
        except Exception:
            pass
    return None

def new_tx_id(prefix="TRF"):
    return f"{prefix}-{BANK_NAME}-{uuid.uuid4().hex[:12].upper()}"

# ──────────────────────────────────────────────
# CARD GENERATION
# ──────────────────────────────────────────────

def random_alphanum(n):
    return "".join(random.choices(string.ascii_uppercase + string.digits, k=n))

def generate_card(cur):
    """XXXX5001XXXX — unique across accounts table."""
    while True:
        card = f"{random_alphanum(4)}{BANK_PORT}{random_alphanum(4)}"
        cur.execute("SELECT 1 FROM accounts WHERE card_number=%s", (card,))
        if cur.fetchone() is None:
            return card

# ──────────────────────────────────────────────
# RECOVERY
# ──────────────────────────────────────────────

def run_recovery():
    try:
        subprocess.run(["python", "jpmc_recovery.py"], check=True, timeout=30)
        log("Recovery complete")
    except Exception as e:
        log(f"Recovery skipped: {e}")

# ──────────────────────────────────────────────
# FIFO WORKER  (deposit + withdraw only)
# ──────────────────────────────────────────────

def fifo_worker():
    log("FIFO worker started")
    while True:
        conn = None
        try:
            conn = connect_db()
            conn.autocommit = False
            cur  = conn.cursor()

            # Grab oldest PENDING request for THIS bank (SKIP LOCKED = deadlock-free)
            cur.execute("""
                SELECT tx_id, account_id, tx_type, amount
                FROM   tx_requests
                WHERE  status = 'PENDING'
                  AND  bank   = %s
                ORDER  BY created_at
                FOR UPDATE SKIP LOCKED
                LIMIT 1
            """, (BANK_NAME,))

            row = cur.fetchone()
            if not row:
                conn.commit()
                conn.close()
                time.sleep(0.5)
                continue

            tx_id, account_id, tx_type, amount = row
            log(f"FIFO processing: {tx_id}  {tx_type}  {amount}")

            cur.execute(
                "UPDATE tx_requests SET status='PROCESSING' WHERE tx_id=%s",
                (tx_id,),
            )
            conn.commit()

            # ── DEPOSIT ───────────────────────────────────
            if tx_type == "DEPOSIT":
                cur.execute("""
                    INSERT INTO transactions(tx_id, tx_type, receiver_account, amount, role, state)
                    VALUES (%s, 'DEPOSIT', %s, %s, 'RECEIVER', 'INIT')
                    ON CONFLICT (tx_id) DO NOTHING
                """, (tx_id, account_id, amount))

                cur.execute("""
                    UPDATE accounts SET balance = balance + %s WHERE account_id = %s
                """, (amount, account_id))

                cur.execute(
                    "UPDATE transactions  SET state='COMMITTED' WHERE tx_id=%s", (tx_id,))
                cur.execute(
                    "UPDATE tx_requests   SET status='DONE'     WHERE tx_id=%s", (tx_id,))
                audit(cur, tx_id, f"DEPOSIT COMMITTED +{amount} → {account_id}")
                conn.commit()
                log(f"DEPOSIT COMMITTED: {tx_id}")

            # ── WITHDRAW ──────────────────────────────────
            elif tx_type == "WITHDRAW":
                cur.execute("""
                    SELECT balance FROM accounts
                    WHERE  account_id = %s
                    FOR UPDATE
                """, (account_id,))
                bal_row = cur.fetchone()

                if bal_row is None or float(bal_row[0]) < float(amount):
                    cur.execute(
                        "UPDATE tx_requests SET status='FAILED' WHERE tx_id=%s", (tx_id,))
                    audit(cur, tx_id, f"WITHDRAW FAILED insufficient funds {account_id}")
                    conn.commit()
                    log(f"WITHDRAW FAILED (insufficient): {tx_id}")
                else:
                    cur.execute("""
                        INSERT INTO transactions(tx_id, tx_type, sender_account, amount, role, state)
                        VALUES (%s, 'WITHDRAW', %s, %s, 'SENDER', 'INIT')
                        ON CONFLICT (tx_id) DO NOTHING
                    """, (tx_id, account_id, amount))

                    cur.execute("""
                        UPDATE accounts SET balance = balance - %s WHERE account_id = %s
                    """, (amount, account_id))

                    cur.execute(
                        "UPDATE transactions SET state='COMMITTED' WHERE tx_id=%s", (tx_id,))
                    cur.execute(
                        "UPDATE tx_requests  SET status='DONE'     WHERE tx_id=%s", (tx_id,))
                    audit(cur, tx_id, f"WITHDRAW COMMITTED -{amount} from {account_id}")
                    conn.commit()
                    log(f"WITHDRAW COMMITTED: {tx_id}")

        except Exception as e:
            log(f"FIFO worker error: {e}")
            try:
                if conn:
                    conn.rollback()
            except Exception:
                pass
            time.sleep(1)
        finally:
            try:
                if conn:
                    conn.close()
            except Exception:
                pass

# ──────────────────────────────────────────────
# HEALTH
# ──────────────────────────────────────────────

@APP.route("/health")
def health():
    return jsonify({"status": "ON", "bank": BANK_NAME}), 200

# ──────────────────────────────────────────────
# SIGNUP
# ──────────────────────────────────────────────

@APP.route("/signup", methods=["POST"])
def signup():
    body = request.get_json(silent=True) or {}
    name = (body.get("customer_name") or "").strip()
    pin  = (body.get("pin")           or "").strip()

    if not name or not pin:
        return jsonify({"status": "FAILED", "reason": "Missing name or PIN"}), 400
    if len(pin) < 4:
        return jsonify({"status": "FAILED", "reason": "PIN must be ≥ 4 characters"}), 400

    pin_hash = bcrypt.hashpw(pin.encode(), bcrypt.gensalt()).decode()

    try:
        conn = connect_db()
        cur  = conn.cursor()
        card = generate_card(cur)

        cur.execute("""
            INSERT INTO accounts(customer_name, card_number, pin_hash, balance, status)
            VALUES (%s, %s, %s, 0, 'ACTIVE')
            RETURNING account_id
        """, (name, card, pin_hash))

        row = cur.fetchone()
        if row is None:
            conn.rollback(); conn.close()
            return jsonify({"status": "FAILED", "reason": "DB insert error"}), 500

        account_id = row[0]
        conn.commit(); conn.close()

        log(f"Signup: {name}  card={card}  account={account_id}")
        return jsonify({
            "status":     "SUCCESS",
            "account_id": str(account_id),
            "card_number": card,
            "bank":        BANK_NAME,
        })
    except Exception as e:
        log(f"Signup error: {e}")
        return jsonify({"status": "FAILED", "reason": str(e)}), 500

# ──────────────────────────────────────────────
# LOGIN
# ──────────────────────────────────────────────

@APP.route("/login", methods=["POST"])
def login():
    body = request.get_json(silent=True) or {}
    card = (body.get("card_number") or "").strip().upper()
    pin  = (body.get("pin")         or "").strip()

    if not card or not pin:
        return jsonify({"status": "FAILED", "reason": "Missing fields"}), 400

    try:
        conn = connect_db()
        cur  = conn.cursor()
        cur.execute("""
            SELECT account_id, customer_name, pin_hash, status
            FROM   accounts
            WHERE  card_number = %s
        """, (card,))
        row = cur.fetchone()
        conn.close()

        if row is None:
            return jsonify({"status": "FAILED", "reason": "Card not found"}), 401

        account_id, name, pin_hash, status = row

        if not bcrypt.checkpw(pin.encode(), pin_hash.encode()):
            return jsonify({"status": "FAILED", "reason": "Wrong PIN"}), 401

        if status != "ACTIVE":
            return jsonify({"status": "FAILED", "reason": "Account inactive"}), 403

        log(f"Login: {name}  account={account_id}")
        return jsonify({
            "status":        "SUCCESS",
            "account_id":    str(account_id),
            "customer_name": name,
            "bank":          BANK_NAME,
            "card_number":   card,
        })
    except Exception as e:
        log(f"Login error: {e}")
        return jsonify({"status": "FAILED", "reason": str(e)}), 500

# ──────────────────────────────────────────────
# USER INFO
# ──────────────────────────────────────────────

@APP.route("/user/<account_id>", methods=["GET"])
def get_user(account_id):
    try:
        conn = connect_db()
        cur  = conn.cursor()
        cur.execute("""
            SELECT account_id, customer_name, card_number, status
            FROM   accounts WHERE account_id = %s
        """, (account_id,))
        row = cur.fetchone()
        conn.close()
        if not row:
            return jsonify({"status": "FAILED", "reason": "Not found"}), 404
        return jsonify({
            "account_id":    str(row[0]),
            "customer_name": row[1],
            "card_number":   row[2],
            "bank":          BANK_NAME,
            "status":        row[3],
        })
    except Exception as e:
        return jsonify({"status": "FAILED", "reason": str(e)}), 500

# ──────────────────────────────────────────────
# BALANCE  (PIN-gated)
# ──────────────────────────────────────────────

@APP.route("/balance", methods=["POST"])
def balance():
    body       = request.get_json(silent=True) or {}
    account_id = body.get("account_id")
    pin        = (body.get("pin") or "").strip()

    if not account_id or not pin:
        return jsonify({"status": "FAILED", "reason": "Missing fields"}), 400

    try:
        conn = connect_db()
        cur  = conn.cursor()
        cur.execute(
            "SELECT balance, pin_hash FROM accounts WHERE account_id = %s",
            (account_id,),
        )
        row = cur.fetchone()
        conn.close()

        if not row:
            return jsonify({"status": "FAILED", "reason": "Account not found"}), 404

        bal, pin_hash = row
        if not bcrypt.checkpw(pin.encode(), pin_hash.encode()):
            return jsonify({"status": "FAILED", "reason": "Wrong PIN"}), 401

        return jsonify({"status": "SUCCESS", "balance": float(bal)})
    except Exception as e:
        return jsonify({"status": "FAILED", "reason": str(e)}), 500

# ──────────────────────────────────────────────
# DEPOSIT  (FIFO queue, PIN-gated)
# ──────────────────────────────────────────────

@APP.route("/deposit", methods=["POST"])
def deposit():
    body       = request.get_json(silent=True) or {}
    account_id = body.get("account_id")
    pin        = (body.get("pin") or "").strip()
    try:
        amount = float(body.get("amount", 0))
    except (TypeError, ValueError):
        return jsonify({"status": "FAILED", "reason": "Invalid amount"}), 400

    if not account_id or not pin or amount <= 0:
        return jsonify({"status": "FAILED", "reason": "Missing or invalid fields"}), 400

    try:
        conn = connect_db()
        cur  = conn.cursor()
        cur.execute("SELECT pin_hash FROM accounts WHERE account_id=%s", (account_id,))
        row = cur.fetchone()
        if not row:
            conn.close()
            return jsonify({"status": "FAILED", "reason": "Account not found"}), 404
        if not bcrypt.checkpw(pin.encode(), row[0].encode()):
            conn.close()
            return jsonify({"status": "FAILED", "reason": "Wrong PIN"}), 401

        tx_id = new_tx_id("DEP")
        cur.execute("""
            INSERT INTO tx_requests(tx_id, account_id, tx_type, amount, status, bank)
            VALUES (%s, %s, 'DEPOSIT', %s, 'PENDING', %s)
        """, (tx_id, account_id, amount, BANK_NAME))
        conn.commit(); conn.close()

        log(f"Deposit queued: {tx_id}  +{amount} → {account_id}")

        # Poll for FIFO completion (max 15 s)
        for _ in range(30):
            time.sleep(0.5)
            c2  = connect_db()
            c2c = c2.cursor()
            c2c.execute("SELECT status FROM tx_requests WHERE tx_id=%s", (tx_id,))
            r = c2c.fetchone()
            c2.close()
            if r and r[0] == "DONE":
                return jsonify({"status": "SUCCESS", "tx_id": tx_id, "amount": amount})
            if r and r[0] == "FAILED":
                return jsonify({"status": "FAILED",  "reason": "Transaction failed", "tx_id": tx_id})

        return jsonify({"status": "QUEUED", "tx_id": tx_id, "message": "Processing in background"})

    except Exception as e:
        log(f"Deposit error: {e}")
        return jsonify({"status": "FAILED", "reason": str(e)}), 500

# ──────────────────────────────────────────────
# WITHDRAW  (FIFO queue, PIN-gated)
# ──────────────────────────────────────────────

@APP.route("/withdraw", methods=["POST"])
def withdraw():
    body       = request.get_json(silent=True) or {}
    account_id = body.get("account_id")
    pin        = (body.get("pin") or "").strip()
    try:
        amount = float(body.get("amount", 0))
    except (TypeError, ValueError):
        return jsonify({"status": "FAILED", "reason": "Invalid amount"}), 400

    if not account_id or not pin or amount <= 0:
        return jsonify({"status": "FAILED", "reason": "Missing or invalid fields"}), 400

    try:
        conn = connect_db()
        cur  = conn.cursor()
        cur.execute(
            "SELECT pin_hash, balance FROM accounts WHERE account_id=%s",
            (account_id,),
        )
        row = cur.fetchone()
        if not row:
            conn.close()
            return jsonify({"status": "FAILED", "reason": "Account not found"}), 404

        pin_hash, bal = row
        if not bcrypt.checkpw(pin.encode(), pin_hash.encode()):
            conn.close()
            return jsonify({"status": "FAILED", "reason": "Wrong PIN"}), 401
        if float(bal) < amount:
            conn.close()
            return jsonify({"status": "FAILED", "reason": "Insufficient funds"}), 400

        tx_id = new_tx_id("WIT")
        cur.execute("""
            INSERT INTO tx_requests(tx_id, account_id, tx_type, amount, status, bank)
            VALUES (%s, %s, 'WITHDRAW', %s, 'PENDING', %s)
        """, (tx_id, account_id, amount, BANK_NAME))
        conn.commit(); conn.close()

        log(f"Withdraw queued: {tx_id}  -{amount} from {account_id}")

        for _ in range(30):
            time.sleep(0.5)
            c2  = connect_db()
            c2c = c2.cursor()
            c2c.execute("SELECT status FROM tx_requests WHERE tx_id=%s", (tx_id,))
            r = c2c.fetchone()
            c2.close()
            if r and r[0] == "DONE":
                return jsonify({"status": "SUCCESS", "tx_id": tx_id, "amount": amount})
            if r and r[0] == "FAILED":
                return jsonify({"status": "FAILED",  "reason": "Insufficient funds", "tx_id": tx_id})

        return jsonify({"status": "QUEUED", "tx_id": tx_id})

    except Exception as e:
        log(f"Withdraw error: {e}")
        return jsonify({"status": "FAILED", "reason": str(e)}), 500

# ──────────────────────────────────────────────
# TRANSFER  (full 2PC)
# ──────────────────────────────────────────────

@APP.route("/transfer", methods=["POST"])
def transfer():
    body          = request.get_json(silent=True) or {}
    sender_id     = body.get("sender_account_id")
    receiver_card = (body.get("receiver_card") or "").strip().upper()
    pin           = (body.get("pin")           or "").strip()
    try:
        amount = float(body.get("amount", 0))
    except (TypeError, ValueError):
        return jsonify({"status": "FAILED", "reason": "Invalid amount"}), 400

    if not sender_id or not receiver_card or not pin or amount <= 0:
        return jsonify({"status": "FAILED", "reason": "Missing or invalid fields"}), 400
    if len(receiver_card) != 12:
        return jsonify({"status": "FAILED", "reason": "Card must be 12 characters"}), 400

    receiver_bank = detect_bank(receiver_card)
    if not receiver_bank:
        return jsonify({"status": "FAILED", "reason": "Unknown bank in card number"}), 400

    tx_id = new_tx_id("TRF")
    log(f"Transfer {tx_id}: {sender_id} → card:{receiver_card} ({receiver_bank})  amount:{amount}")

    # ── Validate sender ────────────────────────────────────────
    try:
        conn = connect_db()
        cur  = conn.cursor()
        cur.execute(
            "SELECT pin_hash, balance FROM accounts WHERE account_id=%s FOR UPDATE",
            (sender_id,),
        )
        row = cur.fetchone()
        if not row:
            conn.rollback(); conn.close()
            return jsonify({"status": "FAILED", "reason": "Sender account not found"}), 404

        pin_hash, bal = row
        if not bcrypt.checkpw(pin.encode(), pin_hash.encode()):
            conn.rollback(); conn.close()
            return jsonify({"status": "FAILED", "reason": "Wrong PIN"}), 401
        if float(bal) < amount:
            conn.rollback(); conn.close()
            return jsonify({"status": "FAILED", "reason": "Insufficient funds"}), 400

        # Record INIT
        cur.execute("""
            INSERT INTO transactions(tx_id, tx_type, sender_account, amount, role, state)
            VALUES (%s, 'TRANSFER', %s, %s, 'SENDER', 'INIT')
            ON CONFLICT (tx_id) DO NOTHING
        """, (tx_id, sender_id, amount))
        audit(cur, tx_id, f"INIT: {sender_id} → {receiver_card} ({receiver_bank})  {amount}")
        conn.commit(); conn.close()
    except Exception as e:
        log(f"Transfer validate error {tx_id}: {e}")
        return jsonify({"status": "FAILED", "reason": str(e)}), 500

    # ── Same bank ──────────────────────────────────────────────
    if receiver_bank == BANK_NAME:
        return _same_bank_2pc(tx_id, sender_id, receiver_card, amount)

    # ── Cross-bank: ask country to route ──────────────────────
    return _cross_bank_2pc(tx_id, sender_id, receiver_card, receiver_bank, amount)


def _same_bank_2pc(tx_id, sender_id, receiver_card, amount):
    """Both accounts in this bank — single DB transaction, no network hop."""
    conn = None
    cur = None
    try:
        conn = connect_db()
        conn.autocommit = False
        cur  = conn.cursor()

        # Lock receiver
        cur.execute(
            "SELECT account_id FROM accounts WHERE card_number=%s FOR UPDATE",
            (receiver_card,),
        )
        rec_row = cur.fetchone()
        if not rec_row:
            cur.execute("UPDATE transactions SET state='ABORTED' WHERE tx_id=%s", (tx_id,))
            audit(cur, tx_id, "ABORT: receiver card not found")
            conn.commit(); conn.close()
            return jsonify({"status": "FAILED", "reason": "Receiver card not found"}), 404

        receiver_id = rec_row[0]
        if str(receiver_id) == str(sender_id):
            conn.rollback(); conn.close()
            return jsonify({"status": "FAILED", "reason": "Cannot transfer to self"}), 400

        # Lock sender balance
        cur.execute(
            "SELECT balance FROM accounts WHERE account_id=%s FOR UPDATE",
            (sender_id,),
        )
        bal_row = cur.fetchone()
        if not bal_row or float(bal_row[0]) < float(amount):
            cur.execute("UPDATE transactions SET state='ABORTED' WHERE tx_id=%s", (tx_id,))
            audit(cur, tx_id, "ABORT: insufficient funds at prepare")
            conn.commit(); conn.close()
            return jsonify({"status": "FAILED", "reason": "Insufficient funds"}), 400

        # PHASE 1 — PREPARE
        cur.execute("UPDATE transactions SET state='PREPARED' WHERE tx_id=%s", (tx_id,))
        cur.execute("""
            INSERT INTO account_locks(tx_id, account_id, amount)
            VALUES (%s, %s, %s) ON CONFLICT DO NOTHING
        """, (tx_id, sender_id, amount))
        audit(cur, tx_id, f"PREPARE: locked sender {sender_id}")
        conn.commit()

        # PHASE 2 — COMMIT
        cur.execute(
            "UPDATE accounts SET balance = balance - %s WHERE account_id=%s",
            (amount, sender_id),
        )
        cur.execute(
            "UPDATE accounts SET balance = balance + %s WHERE account_id=%s",
            (amount, receiver_id),
        )
        cur.execute("UPDATE transactions SET state='COMMITTED' WHERE tx_id=%s", (tx_id,))
        cur.execute("DELETE FROM account_locks WHERE tx_id=%s", (tx_id,))
        audit(cur, tx_id, f"COMMIT: {amount} from {sender_id} to {receiver_id}")
        conn.commit(); conn.close()

        log(f"Same-bank COMMITTED: {tx_id}")
        return jsonify({"status": "SUCCESS", "tx_id": tx_id, "route": "SAME_BANK", "amount": amount})

    except Exception as e:
        log(f"Same-bank 2PC error {tx_id}: {e}")
        try:
            if cur is not None:
                cur.execute("UPDATE transactions  SET state='ABORTED' WHERE tx_id=%s", (tx_id,))
                cur.execute("DELETE FROM account_locks WHERE tx_id=%s", (tx_id,))
            if conn is not None:
                conn.commit(); conn.close()
        except Exception:
            pass
        return jsonify({"status": "FAILED", "reason": str(e)}), 500


def _cross_bank_2pc(tx_id, sender_id, receiver_card, receiver_bank, amount):
    """Sender debit + receiver credit across two bank servers."""

    # 1. Routing ACK from country
    try:
        resp   = country_post("/route_transaction", {
            "sender_bank":   BANK_NAME,
            "receiver_bank": receiver_bank,
            "tx_id":         tx_id,
        })
        result = resp.json()
    except Exception as e:
        _abort(tx_id, f"Country unreachable: {e}")
        return jsonify({"status": "FAILED", "reason": "Country server unreachable"})

    if result.get("status") == "ABORT":
        _abort(tx_id, result.get("reason", "Country ABORT"))
        return jsonify({"status": "FAILED", "reason": result.get("reason", "Routing failed")})

    route = result.get("route")  # "LOCAL" or "CROSS"

    # 2. PHASE 1 — PREPARE sender (debit + lock)
    try:
        conn = connect_db()
        conn.autocommit = False
        cur  = conn.cursor()

        cur.execute(
            "SELECT balance FROM accounts WHERE account_id=%s FOR UPDATE",
            (sender_id,),
        )
        bal_row = cur.fetchone()
        if not bal_row or float(bal_row[0]) < float(amount):
            conn.rollback(); conn.close()
            _abort(tx_id, "Insufficient funds at PREPARE")
            return jsonify({"status": "FAILED", "reason": "Insufficient funds"})

        cur.execute(
            "UPDATE accounts SET balance = balance - %s WHERE account_id=%s",
            (amount, sender_id),
        )
        cur.execute("UPDATE transactions SET state='PREPARED' WHERE tx_id=%s", (tx_id,))
        cur.execute("""
            INSERT INTO account_locks(tx_id, account_id, amount)
            VALUES (%s, %s, %s) ON CONFLICT DO NOTHING
        """, (tx_id, sender_id, amount))
        audit(cur, tx_id, f"PREPARE: debited {amount} from {sender_id}")
        conn.commit(); conn.close()
    except Exception as e:
        log(f"Sender PREPARE error {tx_id}: {e}")
        _abort(tx_id, str(e))
        return jsonify({"status": "FAILED", "reason": str(e)}), 500

    # 3. Ask receiver bank to PREPARE
    prep_resp = bank_post(receiver_bank, "/receive_prepare", {
        "tx_id":         tx_id,
        "receiver_card": receiver_card,
        "amount":        amount,
        "sender_bank":   BANK_NAME,
    })

    if prep_resp is None or prep_resp.json().get("status") != "PREPARED":
        reason = (prep_resp.json().get("reason") if prep_resp else "receiver unreachable")
        log(f"Receiver PREPARE failed {tx_id}: {reason}")
        _refund_and_abort(tx_id, sender_id, amount, f"Receiver prepare failed: {reason}")
        return jsonify({"status": "FAILED", "reason": f"Receiver prepare failed: {reason}"})

    # 4. PHASE 2 — COMMIT receiver
    commit_resp = bank_post(receiver_bank, "/receive_commit", {"tx_id": tx_id})

    if commit_resp is None or commit_resp.json().get("status") != "COMMITTED":
        reason = (commit_resp.json().get("reason") if commit_resp else "receiver unreachable")
        log(f"Receiver COMMIT failed {tx_id}: {reason}")
        # Tell receiver to abort
        bank_post(receiver_bank, "/receive_abort", {"tx_id": tx_id})
        _refund_and_abort(tx_id, sender_id, amount, f"Receiver commit failed: {reason}")
        return jsonify({"status": "FAILED", "reason": f"Commit failed, transaction aborted"})

    # 5. Finalise sender side
    try:
        conn = connect_db()
        cur  = conn.cursor()
        cur.execute("UPDATE transactions SET state='COMMITTED' WHERE tx_id=%s", (tx_id,))
        cur.execute("DELETE FROM account_locks WHERE tx_id=%s", (tx_id,))
        audit(cur, tx_id, f"COMMITTED cross-bank {amount} → {receiver_bank}")
        conn.commit(); conn.close()
    except Exception as e:
        log(f"Sender finalise error {tx_id}: {e}")

    log(f"Cross-bank COMMITTED [{route}]: {tx_id}")
    return jsonify({
        "status": "SUCCESS",
        "tx_id":  tx_id,
        "route":  route,
        "amount": amount,
    })


def _abort(tx_id, reason):
    try:
        conn = connect_db()
        cur  = conn.cursor()
        cur.execute(
            "UPDATE transactions SET state='ABORTED' WHERE tx_id=%s AND state IN ('INIT','PREPARED')",
            (tx_id,),
        )
        cur.execute("DELETE FROM account_locks WHERE tx_id=%s", (tx_id,))
        audit(cur, tx_id, f"ABORTED: {reason}")
        conn.commit(); conn.close()
    except Exception as e:
        log(f"_abort error {tx_id}: {e}")


def _refund_and_abort(tx_id, sender_id, amount, reason):
    try:
        conn = connect_db()
        cur  = conn.cursor()
        cur.execute(
            "UPDATE accounts SET balance = balance + %s WHERE account_id=%s",
            (amount, sender_id),
        )
        cur.execute(
            "UPDATE transactions SET state='ABORTED' WHERE tx_id=%s",
            (tx_id,),
        )
        cur.execute("DELETE FROM account_locks WHERE tx_id=%s", (tx_id,))
        audit(cur, tx_id, f"ABORTED + refunded {amount}: {reason}")
        conn.commit(); conn.close()
        log(f"Refunded sender {sender_id} amount {amount} for {tx_id}")
    except Exception as e:
        log(f"_refund_and_abort error {tx_id}: {e}")

# ──────────────────────────────────────────────
# RECEIVER-SIDE 2PC ENDPOINTS
# (called by the sender bank, not by the client)
# ──────────────────────────────────────────────

@APP.route("/receive_prepare", methods=["POST"])
def receive_prepare():
    """Phase 1: validate receiver card exists, record PREPARED state."""
    body          = request.get_json(silent=True) or {}
    tx_id         = body.get("tx_id")
    receiver_card = (body.get("receiver_card") or "").strip().upper()
    sender_bank   = body.get("sender_bank")
    try:
        amount = float(body.get("amount", 0))
    except (TypeError, ValueError):
        return jsonify({"status": "ABORT", "reason": "Invalid amount"}), 400

    if not tx_id or not receiver_card or amount <= 0:
        return jsonify({"status": "ABORT", "reason": "Missing fields"}), 400

    try:
        conn = connect_db()
        cur  = conn.cursor()
        cur.execute(
            "SELECT account_id FROM accounts WHERE card_number=%s AND status='ACTIVE'",
            (receiver_card,),
        )
        row = cur.fetchone()
        if not row:
            conn.close()
            return jsonify({"status": "ABORT", "reason": "Receiver card not found or inactive"}), 404

        receiver_id = row[0]

        cur.execute("""
            INSERT INTO transactions(tx_id, tx_type, receiver_account, amount, role, state)
            VALUES (%s, 'TRANSFER', %s, %s, 'RECEIVER', 'PREPARED')
            ON CONFLICT (tx_id) DO UPDATE SET state = 'PREPARED'
        """, (tx_id, receiver_id, amount))

        cur.execute("""
            INSERT INTO account_locks(tx_id, account_id, amount)
            VALUES (%s, %s, %s) ON CONFLICT DO NOTHING
        """, (tx_id, receiver_id, amount))

        audit(cur, tx_id, f"RECEIVER PREPARED card:{receiver_card} account:{receiver_id} from {sender_bank}")
        conn.commit(); conn.close()

        log(f"Receive PREPARED: {tx_id} → {receiver_id}")
        return jsonify({"status": "PREPARED", "tx_id": tx_id, "receiver_id": str(receiver_id)})

    except Exception as e:
        log(f"receive_prepare error {tx_id}: {e}")
        return jsonify({"status": "ABORT", "reason": str(e)}), 500


@APP.route("/receive_commit", methods=["POST"])
def receive_commit():
    """Phase 2: credit the receiver account."""
    body  = request.get_json(silent=True) or {}
    tx_id = body.get("tx_id")
    if not tx_id:
        return jsonify({"status": "FAILED", "reason": "Missing tx_id"}), 400

    try:
        conn = connect_db()
        cur  = conn.cursor()

        cur.execute("""
            SELECT receiver_account, amount FROM transactions
            WHERE  tx_id = %s AND role = 'RECEIVER' AND state = 'PREPARED'
            FOR UPDATE
        """, (tx_id,))
        row = cur.fetchone()
        if not row:
            conn.close()
            return jsonify({"status": "FAILED", "reason": "No prepared receiver tx"}), 404

        receiver_id, amount = row

        cur.execute(
            "UPDATE accounts SET balance = balance + %s WHERE account_id=%s",
            (amount, receiver_id),
        )
        cur.execute(
            "UPDATE transactions SET state='COMMITTED' WHERE tx_id=%s AND role='RECEIVER'",
            (tx_id,),
        )
        cur.execute("DELETE FROM account_locks WHERE tx_id=%s", (tx_id,))
        audit(cur, tx_id, f"RECEIVER COMMITTED +{amount} to {receiver_id}")
        conn.commit(); conn.close()

        log(f"Receive COMMITTED: {tx_id}")
        return jsonify({"status": "COMMITTED", "tx_id": tx_id})

    except Exception as e:
        log(f"receive_commit error {tx_id}: {e}")
        return jsonify({"status": "FAILED", "reason": str(e)}), 500


@APP.route("/receive_abort", methods=["POST"])
def receive_abort():
    """Abort the receiver side — no credit was made."""
    body  = request.get_json(silent=True) or {}
    tx_id = body.get("tx_id")
    try:
        conn = connect_db()
        cur  = conn.cursor()
        cur.execute(
            "UPDATE transactions SET state='ABORTED' WHERE tx_id=%s AND role='RECEIVER'",
            (tx_id,),
        )
        cur.execute("DELETE FROM account_locks WHERE tx_id=%s", (tx_id,))
        audit(cur, tx_id, "RECEIVER ABORTED")
        conn.commit(); conn.close()
        return jsonify({"status": "ABORTED"})
    except Exception as e:
        return jsonify({"status": "FAILED", "reason": str(e)}), 500

# ──────────────────────────────────────────────
# TRANSACTION HISTORY
# ──────────────────────────────────────────────

@APP.route("/transactions/<account_id>", methods=["GET"])
def transactions(account_id):
    try:
        conn = connect_db()
        cur  = conn.cursor()
        cur.execute("""
            SELECT tx_id, tx_type, sender_account, receiver_account,
                   amount, role, state, created_at
            FROM   transactions
            WHERE  sender_account = %s OR receiver_account = %s
            ORDER  BY created_at DESC
            LIMIT  30
        """, (account_id, account_id))
        rows = cur.fetchall()
        conn.close()

        txs = [{
            "tx_id":            r[0],
            "tx_type":          r[1],
            "sender_account":   str(r[2]) if r[2] else None,
            "receiver_account": str(r[3]) if r[3] else None,
            "amount":           float(r[4]),
            "role":             r[5],
            "state":            r[6],
            "created_at":       r[7].isoformat() if r[7] else None,
        } for r in rows]

        return jsonify({"status": "SUCCESS", "transactions": txs})
    except Exception as e:
        return jsonify({"status": "FAILED", "reason": str(e)}), 500

# ──────────────────────────────────────────────
# CARD LOOKUP  (used by frontend transfer UI)
# ──────────────────────────────────────────────

@APP.route("/card_lookup", methods=["POST"])
def card_lookup():
    """
    Given a card number, detect its bank.
    If it's this bank → look up locally.
    If it's another bank → ask that bank's /card_lookup_local.
    """
    body = request.get_json(silent=True) or {}
    card = (body.get("card_number") or "").strip().upper()

    if len(card) != 12:
        return jsonify({"status": "FAILED", "reason": "Card must be 12 characters"}), 400

    bank = detect_bank(card)
    if not bank:
        return jsonify({"status": "FAILED", "reason": "Cannot detect bank from card"}), 400

    if bank == BANK_NAME:
        return _local_card_lookup(card)

    # Remote bank
    resp = bank_post(bank, "/card_lookup_local", {"card_number": card}, timeout=3)
    if resp:
        return jsonify(resp.json())
    return jsonify({"status": "OFFLINE", "bank": bank, "reason": f"{bank} unreachable"})


@APP.route("/card_lookup_local", methods=["POST"])
def card_lookup_local():
    """Called by other banks to look up a card on this bank."""
    body = request.get_json(silent=True) or {}
    card = (body.get("card_number") or "").strip().upper()
    return jsonify(_local_card_lookup(card).get_json())


def _local_card_lookup(card):
    try:
        conn = connect_db()
        cur  = conn.cursor()
        cur.execute(
            "SELECT customer_name FROM accounts WHERE card_number=%s",
            (card,),
        )
        row = cur.fetchone()
        conn.close()
        if row:
            return jsonify({"status": "FOUND", "bank": BANK_NAME, "customer_name": row[0]})
        return jsonify({"status": "NOT_FOUND", "bank": BANK_NAME})
    except Exception as e:
        return jsonify({"status": "FAILED", "reason": str(e)})

# ──────────────────────────────────────────────
# MAIN
# ──────────────────────────────────────────────

def start():
    log(f"Starting {BANK_NAME} Bank Server on port {BANK_PORT}")
    run_recovery()
    threading.Thread(target=fifo_worker, daemon=True).start()
    APP.run(host="0.0.0.0", port=BANK_PORT, debug=False)

if __name__ == "__main__":
    start()
