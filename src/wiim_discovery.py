#!/usr/bin/env python3
"""
wiim_discovery.py — mDNS discovery sidecar for the wiim-eq service.

Runs on the host network (so mDNS works, including across VLANs via the
router's mDNS repeater), exposes no ports, and keeps a JSON file of WiiM
devices up to date for the main service to read:

    {"updated": <unix time>, "devices": [{"name", "ip", "source", "last_seen"}]}

Devices that stop answering are kept for WIIM_FORGET_HOURS (default 24) so a
speaker that's briefly asleep doesn't vanish from Home Assistant's dropdowns.

Environment:
  WIIM_DEVICES_FILE   output file (default: data/devices.json)
  WIIM_SCAN_SECONDS   how long each scan listens (default: 5)
  WIIM_SCAN_INTERVAL  seconds between scans (default: 60)
  WIIM_FORGET_HOURS   drop devices not seen for this long (default: 24)
"""

from __future__ import annotations

import json
import logging
import os
import signal
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from wiim_autoeq_web import discover_wiim_devices  # noqa: E402

log = logging.getLogger("wiim_discovery")

OUT = Path(os.environ.get("WIIM_DEVICES_FILE", "data/devices.json"))
SCAN_SECONDS = float(os.environ.get("WIIM_SCAN_SECONDS", "5"))
INTERVAL = float(os.environ.get("WIIM_SCAN_INTERVAL", "60"))
FORGET = float(os.environ.get("WIIM_FORGET_HOURS", "24")) * 3600


def merge(previous: list[dict], found: list[dict], now: float) -> list[dict]:
    """Update last_seen for found devices (keyed by name; IP may change),
    keep recently-seen ones, drop the stale."""
    by_name = {d["name"]: d for d in previous}
    for d in found:
        by_name[d["name"]] = {"name": d["name"], "ip": d["ip"],
                              "source": d.get("source", "linkplay"),
                              "last_seen": int(now)}
    return sorted((d for d in by_name.values() if now - d.get("last_seen", 0) <= FORGET),
                  key=lambda d: d["name"].lower())


def write(devices: list[dict], now: float) -> None:
    OUT.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=OUT.parent, prefix=".tmp-", suffix=".json")
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        json.dump({"updated": int(now), "devices": devices}, fh, indent=2)
    os.chmod(tmp, 0o644)
    os.replace(tmp, OUT)


def main() -> int:
    logging.basicConfig(level=os.environ.get("WIIM_LOG_LEVEL", "INFO"),
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
    try:
        devices = json.loads(OUT.read_text()).get("devices", [])
    except (OSError, ValueError):
        devices = []
    while True:
        now = time.time()
        try:
            found = discover_wiim_devices(timeout=SCAN_SECONDS)
            devices = merge(devices, found, now)
            write(devices, now)
            log.info("scan: %d found, %d known", len(found), len(devices))
        except Exception:  # noqa: BLE001 — keep the sidecar alive
            log.exception("scan failed")
        time.sleep(INTERVAL)


if __name__ == "__main__":
    sys.exit(main())
