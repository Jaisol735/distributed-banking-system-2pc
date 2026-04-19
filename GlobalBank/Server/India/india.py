"""
india.py  —  India Country Server  (port 5000)
================================================
Responsibilities:
  • Monitor health of SBI (5001) and HDFC (5002)
  • Register with World server on startup
  • Route same-country transfers  → ACK only, banks do 2PC
  • Route cross-country transfers → forward to World server
  • Expose /check_bank for World server queries

NOTE: Country server is a ROUTING / HEALTH layer only.
      Two-Phase Commit is executed exclusively by the bank servers.
"""

import os
import sys
import json
import time
import threading
import requests
import subprocess
from datetime import datetime
from flask import Flask, request, jsonify

# Load Tailscale IP from shared config (Server/config.json)
BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE_DIR)
from config_loader import server_urls, PORTS, TAILSCALE_IP

# ──────────────────────────────────────────────
# CONFIG
# ──────────────────────────────────────────────

COUNTRY_NAME    = "India"
COUNTRY_PORT    = PORTS["india"]
REGISTRY_FILE   = "india_registry.json"

WORLD_URLS = server_urls(PORTS["world"])

WORLD_DIRECTORY_FILE = os.path.join(BASE_DIR, "world_directory.json")

FAIL_THRESHOLD = 3   # consecutive failures before marking bank OFF

APP = Flask(__name__)

# ──────────────────────────────────────────────
# UTILITIES
# ──────────────────────────────────────────────

def ts():
    return datetime.now().strftime("%H:%M:%S.%f")[:-3]

def log(msg):
    print(f"[{COUNTRY_NAME}][{ts()}] {msg}")

def load_registry():
    with open(REGISTRY_FILE) as f:
        return json.load(f)

def save_registry(data):
    with open(REGISTRY_FILE, "w") as f:
        json.dump(data, f, indent=2)

def load_world_directory():
    with open(WORLD_DIRECTORY_FILE) as f:
        return json.load(f)

def find_bank_country(bank_name):
    directory = load_world_directory()
    for country, info in directory.items():
        if bank_name in info.get("banks", {}):
            return country
    return None

def world_post(path, payload=None, timeout=4):
    last_err = None
    for url in WORLD_URLS:
        try:
            r = requests.post(f"{url}{path}", json=payload, timeout=timeout)
            return r
        except Exception as e:
            last_err = e
    raise ConnectionError(f"All world endpoints unreachable: {last_err}")

# ──────────────────────────────────────────────
# RECOVERY
# ──────────────────────────────────────────────

def run_recovery():
    try:
        subprocess.run(["python", "india_recovery.py"], check=True, timeout=30)
        log("Recovery complete")
    except Exception as e:
        log(f"Recovery skipped: {e}")

# ──────────────────────────────────────────────
# BANK HEALTH MONITOR  (background thread)
# ──────────────────────────────────────────────

def bank_health_monitor():
    log("Bank health monitor started")
    while True:
        data = load_registry()
        for bank, info in data["banks"].items():
            port    = info["port"]
            alive   = False
            base_urls = server_urls(port)
            for base in base_urls:
                try:
                    r = requests.get(f"{base}/health", timeout=2)
                    if r.status_code == 200 and r.json().get("status") == "ON":
                        alive = True
                        break
                except Exception:
                    pass

            if alive:
                if info.get("status") != "ON":
                    log(f"Bank {bank} is now ON")
                info["status"]     = "ON"
                info["fail_count"] = 0
            else:
                info["fail_count"] = info.get("fail_count", 0) + 1
                if info["fail_count"] >= FAIL_THRESHOLD:
                    if info.get("status") != "OFF":
                        log(f"Bank {bank} marked OFF (fail_count={info['fail_count']})")
                    info["status"] = "OFF"

        save_registry(data)
        log(f"Bank statuses: { {b: i['status'] for b, i in data['banks'].items()} }")
        time.sleep(5)

# ──────────────────────────────────────────────
# WORLD ACK  (background thread — retries until success)
# ──────────────────────────────────────────────

def notify_world_loop():
    while True:
        try:
            r = world_post("/country_ack", {"country": COUNTRY_NAME}, timeout=3)
            if r.status_code == 200:
                log("Registered with World server")
                break
            log(f"World ACK failed: {r.status_code} {r.text}")
        except Exception as e:
            log(f"World unreachable, retrying... ({e})")
        time.sleep(5)

# ──────────────────────────────────────────────
# API ENDPOINTS
# ──────────────────────────────────────────────

@APP.route("/health", methods=["GET"])
def health():
    return jsonify({"status": "ON", "country": COUNTRY_NAME}), 200


@APP.route("/country_status", methods=["GET"])
def country_status():
    registry = load_registry()
    return jsonify({
        "country": COUNTRY_NAME,
        "status":  "ON",
        "banks":   {b: i.get("status", "UNKNOWN") for b, i in registry["banks"].items()}
    }), 200


@APP.route("/check_bank", methods=["POST"])
def check_bank():
    """
    Called by World server to verify a bank is online.
    """
    body = request.get_json(silent=True) or {}
    bank_name = body.get("bank")
    if not bank_name:
        return jsonify({"bank_status": "OFF"}), 400

    registry = load_registry()
    info     = registry["banks"].get(bank_name)
    if not info:
        return jsonify({"bank_status": "OFF"}), 200

    return jsonify({"bank_status": info.get("status", "OFF")}), 200


@APP.route("/route_transaction", methods=["POST"])
def route_transaction():
    """
    Called by the sender bank before initiating a 2PC transfer.

    Returns:
      { "route": "LOCAL",  "country": "India" }          — same country, banks talk directly
      { "route": "CROSS",  "via": "WORLD", ... }          — cross-country, World validated
      { "status": "ABORT", "reason": "..." }               — something is offline
    """
    body = request.get_json(silent=True) or {}
    sender_bank   = body.get("sender_bank")
    receiver_bank = body.get("receiver_bank")

    if not sender_bank or not receiver_bank:
        return jsonify({"status": "ABORT", "reason": "Missing bank info"}), 400

    sender_country   = find_bank_country(sender_bank)
    receiver_country = find_bank_country(receiver_bank)

    if not sender_country or not receiver_country:
        return jsonify({"status": "ABORT", "reason": "Unknown bank(s)"}), 400

    # ── SAME COUNTRY ──────────────────────────────
    if sender_country == receiver_country == COUNTRY_NAME:
        registry = load_registry()
        banks    = registry["banks"]

        s_status = banks.get(sender_bank,   {}).get("status", "OFF")
        r_status = banks.get(receiver_bank, {}).get("status", "OFF")

        if s_status != "ON":
            return jsonify({"status": "ABORT", "reason": f"{sender_bank} is OFFLINE"}), 200
        if r_status != "ON":
            return jsonify({"status": "ABORT", "reason": f"{receiver_bank} is OFFLINE"}), 200

        log(f"LOCAL route approved: {sender_bank} → {receiver_bank}")
        return jsonify({"route": "LOCAL", "country": COUNTRY_NAME}), 200

    # ── CROSS-COUNTRY ─────────────────────────────
    log(f"Cross-country: {sender_country} → {receiver_country}, asking World")
    try:
        r = world_post("/connect_countries", {
            "from_country":  sender_country,
            "to_country":    receiver_country,
            "receiver_bank": receiver_bank,
        }, timeout=5)

        body_r = r.json()
        if body_r.get("status") == "READY":
            log(f"CROSS route approved via World: {sender_bank} → {receiver_bank}")
            return jsonify({
                "route":          "CROSS",
                "via":            "WORLD",
                "target_country": receiver_country,
            }), 200

        reason = body_r.get("reason", "World ABORT")
        log(f"ABORT from World: {reason}")
        return jsonify({"status": "ABORT", "reason": reason}), 200

    except Exception as e:
        log(f"World unreachable: {e}")
        return jsonify({"status": "ABORT", "reason": "World server unreachable"}), 200


# ──────────────────────────────────────────────
# MAIN
# ──────────────────────────────────────────────

def start():
    log("India country server starting")
    run_recovery()
    threading.Thread(target=notify_world_loop,   daemon=True).start()
    threading.Thread(target=bank_health_monitor, daemon=True).start()
    APP.run(host="0.0.0.0", port=COUNTRY_PORT, debug=False)

if __name__ == "__main__":
    start()
