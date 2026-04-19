"""
world.py  —  World Server  (port 4000)
=========================================
Responsibilities:
  • Receive country ACK registrations
  • Health-monitor all country servers
  • Validate cross-country transfers:
      - Is the destination country online?
      - Is the destination bank online?
  • Persist state to world_log.json

NOTE: World is a ROUTING / VALIDATION layer only.
      Two-Phase Commit is executed exclusively by the bank servers.
"""

import json
import sys
import os
import time
import threading
import requests
import subprocess
from datetime import datetime
from flask import Flask, request, jsonify

# Load Tailscale IP from shared config
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from config_loader import server_urls, PORTS, TAILSCALE_IP

# ──────────────────────────────────────────────
# CONFIG
# ──────────────────────────────────────────────

WORLD_PORT = PORTS["world"]
LOG_FILE   = "world_log.json"

# Try Tailscale IP first, then localhost — works both same-machine and cross-machine
COUNTRY_SERVERS = {
    "India": server_urls(PORTS["india"]),
    "USA":   server_urls(PORTS["usa"]),
}

APP = Flask(__name__)

# ──────────────────────────────────────────────
# UTILITIES
# ──────────────────────────────────────────────

def ts():
    return datetime.now().strftime("%H:%M:%S.%f")[:-3]

def log(msg):
    print(f"[WORLD][{ts()}] {msg}")

def load_log():
    try:
        with open(LOG_FILE) as f:
            return json.load(f)
    except Exception:
        return {"countries": {}, "transactions": []}

def save_log(data):
    try:
        with open(LOG_FILE, "w") as f:
            json.dump(data, f, indent=2)
    except Exception as e:
        log(f"save_log error: {e}")

def country_get(country, path, timeout=3):
    """Try all endpoints for a country, return first success."""
    for url in COUNTRY_SERVERS.get(country, []):
        try:
            r = requests.get(f"{url}{path}", timeout=timeout)
            if r.status_code == 200:
                return r
        except Exception:
            pass
    return None

def country_post(country, path, payload=None, timeout=4):
    """Try all endpoints for a country, return first success."""
    for url in COUNTRY_SERVERS.get(country, []):
        try:
            r = requests.post(f"{url}{path}", json=payload, timeout=timeout)
            if r.status_code:
                return r
        except Exception:
            pass
    return None

# ──────────────────────────────────────────────
# RECOVERY
# ──────────────────────────────────────────────

def run_recovery():
    log("Running world recovery...")
    try:
        subprocess.run(["python", "world_recovery.py"], check=True, timeout=30)
        log("Recovery complete")
    except Exception as e:
        log(f"Recovery skipped: {e}")

# ──────────────────────────────────────────────
# COUNTRY HEALTH MONITOR  (background thread)
# ──────────────────────────────────────────────

def country_health_monitor():
    log("Country health monitor started")
    while True:
        data = load_log()
        for country, urls in COUNTRY_SERVERS.items():
            alive = False
            for url in urls:
                try:
                    r = requests.get(f"{url}/health", timeout=2)
                    if r.status_code == 200 and r.json().get("status") == "ON":
                        alive = True
                        break
                except Exception:
                    pass

            prev = data["countries"].get(country, "OFF")
            data["countries"][country] = "ON" if alive else "OFF"

            if prev != data["countries"][country]:
                log(f"{country}: {prev} → {data['countries'][country]}")

        log(f"Country status: {data['countries']}")
        save_log(data)
        time.sleep(5)

# ──────────────────────────────────────────────
# API ENDPOINTS
# ──────────────────────────────────────────────

@APP.route("/health", methods=["GET"])
def health():
    return jsonify({"status": "ON", "server": "WORLD"}), 200


@APP.route("/country_ack", methods=["POST"])
def country_ack():
    """
    Called by country servers on startup to announce they are online.
    """
    body = request.get_json(silent=True) or {}
    country = body.get("country")
    if not country:
        return jsonify({"status": "ERROR", "reason": "Missing country"}), 400

    data = load_log()
    data["countries"][country] = "ON"
    save_log(data)

    log(f"ACK received from {country}")
    return jsonify({"status": "ACK_OK"}), 200


@APP.route("/world_status", methods=["GET"])
def world_status():
    """Return current country status map."""
    data = load_log()
    return jsonify({
        "server": "WORLD",
        "countries": data.get("countries", {})
    }), 200


@APP.route("/connect_countries", methods=["POST"])
def connect_countries():
    """
    Called by the sender's country server when a cross-country transfer is needed.

    Validates:
      1. Destination country is online (per health monitor)
      2. Destination bank is online (asks destination country server)

    Returns READY or ABORT — does NOT execute any money movement.
    """
    body = request.get_json(silent=True) or {}
    from_country  = body.get("from_country")
    to_country    = body.get("to_country")
    receiver_bank = body.get("receiver_bank")

    if not from_country or not to_country or not receiver_bank:
        return jsonify({"status": "ABORT", "reason": "Missing fields"}), 400

    log(f"connect_countries: {from_country} → {to_country}, bank={receiver_bank}")

    # 1. Check destination country is online
    data = load_log()
    if data["countries"].get(to_country) != "ON":
        reason = f"{to_country} country server is OFFLINE"
        log(f"ABORT: {reason}")
        return jsonify({"status": "ABORT", "reason": reason}), 200

    # 2. Ask destination country to confirm bank status
    resp = country_post(to_country, "/check_bank", {"bank": receiver_bank}, timeout=4)
    if resp is None:
        reason = f"Cannot reach {to_country} country server"
        log(f"ABORT: {reason}")
        return jsonify({"status": "ABORT", "reason": reason}), 200

    result = resp.json()
    if result.get("bank_status") != "ON":
        reason = f"{receiver_bank} bank is OFFLINE in {to_country}"
        log(f"ABORT: {reason}")
        return jsonify({"status": "ABORT", "reason": reason}), 200

    log(f"READY: {from_country} → {to_country}/{receiver_bank}")
    return jsonify({"status": "READY", "route": "CROSS"}), 200


# ──────────────────────────────────────────────
# MAIN
# ──────────────────────────────────────────────

def start():
    log("World server starting")
    run_recovery()
    threading.Thread(target=country_health_monitor, daemon=True).start()
    APP.run(host="0.0.0.0", port=WORLD_PORT, debug=False)

if __name__ == "__main__":
    start()
