"""
config_loader.py  —  shared helper, placed in Server/
All servers import this to get the Tailscale IP from one place.

Usage (from any sub-folder):
    import sys, os
    sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))  # adjust depth
    from config_loader import TAILSCALE_IP, server_urls
"""

import json
import os

_HERE = os.path.dirname(os.path.abspath(__file__))
_CFG_FILE = os.path.join(_HERE, "config.json")

def _load():
    with open(_CFG_FILE) as f:
        return json.load(f)

cfg = _load()
TAILSCALE_IP = cfg["tailscale_ip"]
port_map = cfg["ports"]
# support case-insensitive port names (e.g. "india" or "India")
for key, value in list(port_map.items()):
    port_map[key.lower()] = value
PORTS = port_map

def server_urls(port):
    """
    Return list of URLs to try for a given port.
    Order: Tailscale IP first (works cross-machine), then localhost (works same machine).
    """
    urls = []
    if TAILSCALE_IP and TAILSCALE_IP != "127.0.0.1":
        urls.append(f"http://{TAILSCALE_IP}:{port}")
    urls.append(f"http://127.0.0.1:{port}")
    return urls
