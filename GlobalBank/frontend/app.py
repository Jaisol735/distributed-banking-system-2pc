"""
app.py  —  GlobalBank Frontend Server
========================================
Run this on PC-1 (the browser machine).

Usage:
    python app.py --server-ip <Tailscale IP of PC-2>

Example:
    python app.py --server-ip 100.64.0.1

Install:
    pip install flask requests

How it works:
    ┌─────────┐  localhost:8080  ┌──────────────────┐  Tailscale  ┌───────────────┐
    │ Browser │ ───────────────▶ │  app.py  (PC-1)  │ ──────────▶ │ Banks  (PC-2) │
    └─────────┘                  └──────────────────┘             └───────────────┘

    The browser ONLY ever talks to localhost — zero CORS issues.
    Flask proxies every /proxy/<port>/... call to the real bank server on PC-2.

Endpoints:
    GET  /                        → serves index.html
    GET  /config                  → ports map (called once on page load)
    GET  /bank_config/<BANK>      → Supabase host/db/user for that bank
                                    (password is NEVER sent to the browser)
    POST /proxy/<port>/<path>     → reverse-proxy to bank server
    GET  /proxy/<port>/<path>     → reverse-proxy to bank server

supabase.json location:  ../Server/supabase.json  (relative to this file)
"""

import argparse
import os
import json
import requests
from flask import Flask, render_template, request, jsonify, Response

app = Flask(__name__)

# ── resolved at startup via --server-ip ───────────────
SERVER_IP = "127.0.0.1"

BANK_PORTS    = {"SBI": 5001, "HDFC": 5002, "JPMC": 6001, "BOA": 6002}
COUNTRY_PORTS = {"India": 5000, "USA": 6000}
WORLD_PORT    = 4000

# Path to Server/supabase.json — one directory up from frontend/
_HERE         = os.path.dirname(os.path.abspath(__file__))
SUPABASE_FILE = os.path.join(_HERE, "..", "Server", "supabase.json")


def _load_supabase():
    """
    Load Server/supabase.json.
    Returns dict keyed by bank name, e.g. {"SBI": {...}, "HDFC": {...}, ...}
    Returns {} if file not found or unreadable.
    """
    try:
        with open(SUPABASE_FILE) as f:
            return json.load(f)
    except FileNotFoundError:
        print("[app.py] WARNING: Server/supabase.json not found")
        return {}
    except Exception as e:
        print(f"[app.py] WARNING: could not read supabase.json: {e}")
        return {}


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/config")
def config():
    return jsonify({
        "server_ip":     SERVER_IP,
        "bank_ports":    BANK_PORTS,
        "country_ports": COUNTRY_PORTS,
        "world_port":    WORLD_PORT,
    })


@app.route("/bank_config/<bank_name>")
def bank_config(bank_name):
    bank_name = bank_name.upper()

    if bank_name not in BANK_PORTS:
        return jsonify({
            "status": "FAILED",
            "reason": f"Unknown bank: {bank_name}"
        }), 404

    db    = _load_supabase()
    entry = db.get(bank_name)

    if not entry:
        # supabase.json exists but this bank has no entry
        return jsonify({
            "status":   "OK",
            "bank":     bank_name,
            "host":     "not configured",
            "port":     5432,
            "database": "postgres",
            "user":     "postgres",
        })

    # Return config — password intentionally excluded
    return jsonify({
        "status":   "OK",
        "bank":     bank_name,
        "host":     entry.get("host", ""),
        "port":     entry.get("port", 5432),
        "database": entry.get("database", "postgres"),
        "user":     entry.get("user", "postgres"),
        # "password" key deliberately absent
    })


@app.route("/proxy/<int:port>/<path:endpoint>", methods=["GET", "POST", "OPTIONS"])
def proxy(port, endpoint):
    target = f"http://{SERVER_IP}:{port}/{endpoint}"

    try:
        if request.method == "POST":
            resp = requests.post(
                target,
                json=request.get_json(silent=True),
                timeout=20,
            )
        else:
            resp = requests.get(target, timeout=10)

        return Response(
            resp.content,
            status=resp.status_code,
            content_type=resp.headers.get("Content-Type", "application/json"),
        )

    except requests.exceptions.ConnectionError:
        return jsonify({
            "status": "FAILED",
            "reason": f"Cannot reach {SERVER_IP}:{port} — is the bank server running?",
        }), 503

    except requests.exceptions.Timeout:
        return jsonify({
            "status": "FAILED",
            "reason": f"Request to {SERVER_IP}:{port} timed out.",
        }), 504

    except Exception as e:
        return jsonify({"status": "FAILED", "reason": str(e)}), 500


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="GlobalBank Frontend Server")
    parser.add_argument(
        "--server-ip",
        default="127.0.0.1",
        help="Tailscale IP of PC-2 where bank servers run (e.g. 100.64.0.1)",
    )
    parser.add_argument(
        "--port", type=int, default=8080,
        help="Port for this frontend server (default: 8080)",
    )
    args = parser.parse_args()
    SERVER_IP = args.server_ip

    print(f"""
╔══════════════════════════════════════════════════╗
║         GlobalBank Frontend Server               ║
╠══════════════════════════════════════════════════╣
║  Browser URL   :  http://localhost:{args.port:<13}║
║  Bank servers  :  {SERVER_IP:<28}║
║  Supabase cfg  :  ../Server/supabase.json        ║
╚══════════════════════════════════════════════════╝
    """)

    app.run(host="0.0.0.0", port=args.port, debug=False)
