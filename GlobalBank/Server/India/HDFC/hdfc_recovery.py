"""
hdfc_recovery.py  —  HDFC Bank Recovery
==========================================
Runs automatically at startup before Flask begins serving.

Actions:
  1. PREPARED sender transactions   → refund balance + ABORT
  2. PREPARED receiver transactions → ABORT (no credit was made)
  3. PROCESSING tx_requests         → reset to PENDING (re-queue for FIFO)
"""

import json
import psycopg2
from datetime import datetime

LOG_FILE  = "bank_log.json"
BANK_NAME = "HDFC"


def log(msg):
    print(f"[HDFC-RECOVERY][{datetime.now().strftime('%H:%M:%S')}] {msg}")


def load_cfg():
    with open(LOG_FILE) as f:
        return json.load(f)


def connect_db(cfg):
    return psycopg2.connect(
        host=cfg["host"],
        port=cfg["port"],
        database=cfg["database"],
        user=cfg["user"],
        password=cfg["password"],
        connect_timeout=10,
    )


def recover():
    data = load_cfg()
    cfg  = data.get("supabase")
    if not cfg:
        log("No supabase config in bank_log.json — skipping DB recovery")
        return

    try:
        conn = connect_db(cfg)
        conn.autocommit = False
        cur  = conn.cursor()

        # ── 1. PREPARED SENDERS: debit already done, outcome unknown → refund ──
        cur.execute("""
            SELECT tx_id, sender_account, amount
            FROM   transactions
            WHERE  state = 'PREPARED'
              AND  role  = 'SENDER'
        """)
        rows = cur.fetchall()
        log(f"Found {len(rows)} PREPARED sender transaction(s)")

        for tx_id, sender_id, amount in rows:
            log(f"  Refunding sender {sender_id}  amount={amount}  tx={tx_id}")
            cur.execute(
                "UPDATE accounts SET balance = balance + %s WHERE account_id = %s",
                (amount, sender_id),
            )
            cur.execute(
                "UPDATE transactions SET state = 'ABORTED' WHERE tx_id = %s AND role = 'SENDER'",
                (tx_id,),
            )
            cur.execute("DELETE FROM account_locks WHERE tx_id = %s", (tx_id,))
            cur.execute(
                "INSERT INTO audit_log(tx_id, message) VALUES (%s, %s)",
                (tx_id, f"RECOVERY: ABORTED sender, refunded {amount}"),
            )

        # ── 2. PREPARED RECEIVERS: prepare done but commit unknown → ABORT ──
        cur.execute("""
            SELECT tx_id, receiver_account
            FROM   transactions
            WHERE  state = 'PREPARED'
              AND  role  = 'RECEIVER'
        """)
        rows = cur.fetchall()
        log(f"Found {len(rows)} PREPARED receiver transaction(s)")

        for tx_id, receiver_id in rows:
            log(f"  Aborting receiver {receiver_id}  tx={tx_id}")
            cur.execute(
                "UPDATE transactions SET state = 'ABORTED' WHERE tx_id = %s AND role = 'RECEIVER'",
                (tx_id,),
            )
            cur.execute("DELETE FROM account_locks WHERE tx_id = %s", (tx_id,))
            cur.execute(
                "INSERT INTO audit_log(tx_id, message) VALUES (%s, %s)",
                (tx_id, "RECOVERY: ABORTED receiver (sender status unknown)"),
            )

        # ── 3. PROCESSING tx_requests: FIFO worker crashed mid-flight → re-queue ──
        cur.execute("""
            UPDATE tx_requests
               SET status = 'PENDING'
             WHERE status = 'PROCESSING'
               AND bank   = %s
        """, (BANK_NAME,))
        n = cur.rowcount
        if n:
            log(f"Reset {n} PROCESSING request(s) to PENDING for FIFO re-processing")

        conn.commit()
        conn.close()
        log("Recovery complete")

    except Exception as e:
        log(f"Recovery error: {e}")


if __name__ == "__main__":
    log("Recovery started")
    recover()
    log("Recovery finished")
