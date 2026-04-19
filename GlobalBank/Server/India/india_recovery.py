"""
india_recovery.py  —  India Country Server Recovery
=====================================================
Runs automatically when india.py starts.
Resets all bank statuses to OFF so the health monitor
rediscovers them cleanly on startup.
"""

import json
from datetime import datetime

REGISTRY_FILE = "india_registry.json"


def log(msg):
    print(f"[INDIA-RECOVERY][{datetime.now().strftime('%H:%M:%S')}] {msg}")


def load_registry():
    try:
        with open(REGISTRY_FILE) as f:
            return json.load(f)
    except Exception as e:
        log(f"Cannot read registry: {e}")
        return {"country": "India", "banks": {}}


def save_registry(data):
    with open(REGISTRY_FILE, "w") as f:
        json.dump(data, f, indent=2)


def recover():
    data = load_registry()
    for bank, info in data.get("banks", {}).items():
        info["status"]     = "OFF"
        info["fail_count"] = 0
        log(f"Reset {bank}: status=OFF, fail_count=0")
    save_registry(data)
    log("Registry reset complete")


if __name__ == "__main__":
    log("India recovery started")
    recover()
    log("India recovery finished")
