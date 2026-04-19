"""
world_recovery.py  —  World Server Recovery
=============================================
Runs automatically when world.py starts.

Actions:
  • Resets all country statuses to OFF  (health monitor re-discovers on startup)
  • Removes any stuck in-progress transaction records from world_log.json
"""

import json
from datetime import datetime

LOG_FILE = "world_log.json"


def log(msg):
    print(f"[WORLD-RECOVERY][{datetime.now().strftime('%H:%M:%S')}] {msg}")


def load_log():
    try:
        with open(LOG_FILE) as f:
            return json.load(f)
    except Exception:
        log("world_log.json not found — creating fresh")
        return {"countries": {}, "transactions": []}


def save_log(data):
    with open(LOG_FILE, "w") as f:
        json.dump(data, f, indent=2)


def recover():
    data = load_log()

    # Reset every country to OFF — health monitor will re-detect them
    old = dict(data.get("countries", {}))
    data["countries"] = {c: "OFF" for c in old}
    log(f"Reset country statuses to OFF: {list(data['countries'].keys())}")

    # Remove transaction records that are not in a terminal state
    txs   = data.get("transactions", [])
    stuck = [t for t in txs if t.get("state") not in ("COMMITTED", "ABORTED")]
    if stuck:
        log(f"Removing {len(stuck)} stuck transaction record(s)")
        data["transactions"] = [
            t for t in txs if t.get("state") in ("COMMITTED", "ABORTED")
        ]
    else:
        log("No stuck transactions found")

    save_log(data)
    log("World log cleaned")


if __name__ == "__main__":
    log("World recovery started")
    recover()
    log("World recovery finished")
