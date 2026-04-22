#!/usr/bin/env python3
"""
wiim_autoeq_web.py — a local web UI for wiim_autoeq.py

Runs a tiny Flask server at http://127.0.0.1:5173/ with a single-page UI:
  1) enter your WiiM's IP and click "Test connection"
  2) search for your headphone from the AutoEQ list (~6000 models)
  3) click "Apply PEQ" to push the profile to your WiiM

Why a local server instead of a browser-only app? Because the WiiM's HTTP API
uses a self-signed certificate, and browsers refuse to talk to it. The same
CORS/TLS wall killed earlier browser-based attempts. Running Python locally
side-steps both problems completely.

USAGE
-----
    pip install flask requests
    python3 wiim_autoeq_web.py
    # then open http://127.0.0.1:5173/ in your browser

The UI and API logic both re-use the core from wiim_autoeq.py (must be in the
same directory).
"""

from __future__ import annotations

import argparse
import logging
import re
import sys
import traceback
import urllib.parse
from pathlib import Path

try:
    from flask import Flask, jsonify, render_template_string, request
except ImportError:
    print("error: this script needs Flask.\n    pip install flask requests",
          file=sys.stderr)
    sys.exit(2)

# zeroconf is optional — if it's not installed, the UI falls back to manual
# IP entry. Install it with `pip install zeroconf` for device discovery.
try:
    from zeroconf import ServiceBrowser, ServiceListener, Zeroconf
    _ZEROCONF_AVAILABLE = True
except ImportError:
    _ZEROCONF_AVAILABLE = False

# Pull in everything we already built in the CLI — parser, WiiM client,
# AutoEQ fetcher, constants.
sys.path.insert(0, str(Path(__file__).resolve().parent))
try:
    from wiim_autoeq import (  # noqa: E402
        AUTOEQ_RAW_BASE,
        AUTOEQ_RESULTS_README,
        VALID_SOURCES,
        PeqBand,
        Profile,
        WiimClient,
        parse_profile,
        requests,  # re-use the same requests import (and its urllib3 warning suppression)
    )
except ImportError as e:
    print(f"error: couldn't import wiim_autoeq.py from the same directory: {e}",
          file=sys.stderr)
    sys.exit(2)


app = Flask(__name__)

# In-memory cache of the AutoEQ headphone index. Populated on first use.
# Structure: list of {"name": str, "path": str}  (path relative to results/)
_HEADPHONE_CACHE: list[dict] | None = None


# ──────────────────────────────────────────────────────────────────────────────
# AutoEQ index loading
# ──────────────────────────────────────────────────────────────────────────────

def load_headphone_index() -> list[dict]:
    """Fetch & parse AutoEQ's results/README.md into a list of headphones."""
    global _HEADPHONE_CACHE
    if _HEADPHONE_CACHE is not None:
        return _HEADPHONE_CACHE

    r = requests.get(AUTOEQ_RESULTS_README, timeout=20)
    r.raise_for_status()
    body = r.text

    # Match markdown links. The URL may contain balanced (…) from headphone names
    # (e.g. "Sony WH-1000XM4 (2021)"), so we use a pattern that allows one level
    # of nested parens instead of a lazy \S+? that stops at the first ')'.
    link_re = re.compile(r"\[([^\]]+)\]\(\.?/?((?:[^()\s]|\([^)]*\))*)\)")
    seen = set()
    items: list[dict] = []
    for name, path in link_re.findall(body):
        decoded = urllib.parse.unquote(path).lstrip("./")
        # Skip non-folder links (images, anchors, etc.) and known meta links.
        if "/" not in decoded or decoded.endswith(".md"):
            continue
        key = (name, decoded)
        if key in seen:
            continue
        seen.add(key)
        items.append({"name": name, "path": decoded})

    # Sort alphabetically, case-insensitively.
    items.sort(key=lambda d: d["name"].lower())
    _HEADPHONE_CACHE = items
    return items


def fetch_profile_from_path(folder_path: str) -> tuple[str, str]:
    """Given an AutoEQ folder path like 'oratory1990/over-ear/Sennheiser HD 600',
    fetch the ParametricEQ.txt and return (text, url)."""
    leaf = folder_path.rstrip("/").rsplit("/", 1)[-1]
    file_path = f"{folder_path.rstrip('/')}/{leaf} ParametricEQ.txt"
    url = f"{AUTOEQ_RAW_BASE}/results/{urllib.parse.quote(file_path, safe='/()')}"
    r = requests.get(url, timeout=20)
    if r.status_code != 200:
        raise RuntimeError(
            f"AutoEQ returned HTTP {r.status_code} for that headphone. "
            f"The folder may not contain a ParametricEQ.txt file.")
    return r.text, url


# ──────────────────────────────────────────────────────────────────────────────
# mDNS discovery — finds WiiM devices advertising on _linkplay._tcp.local.
# ──────────────────────────────────────────────────────────────────────────────

# The _linkplay._tcp type is what WiiM uses (confirmed from a WiiM forum
# packet capture). We also browse _raop._tcp (AirPlay) as a fallback, since
# WiiMs advertise there too and the instance name is human-readable.
_MDNS_SERVICE_TYPES = [
    "_linkplay._tcp.local.",
    "_raop._tcp.local.",  # AirPlay — service name is like "xxxxxx@WiiM Pro"
]


class _WiimDiscoveryListener:
    """Collects service info as zeroconf discovers entries. We keep a dict
    keyed by IP so that the same device showing up on two service types
    (linkplay + raop) collapses into one entry."""
    def __init__(self):
        # ip -> {"ip": str, "name": str, "source": "linkplay"|"airplay"}
        self.devices: dict[str, dict] = {}

    def add_service(self, zc, type_, name):
        try:
            info = zc.get_service_info(type_, name, timeout=1500)
        except Exception:
            return
        if not info or not info.addresses:
            return
        # Convert the first IPv4 address to dotted-quad string.
        import socket
        ip = None
        for addr in info.addresses:
            if len(addr) == 4:  # IPv4
                ip = socket.inet_ntoa(addr)
                break
        if not ip:
            return

        # Pick a human-readable name. For _linkplay, the instance name
        # ("WiiM Ultra-ABC1._linkplay._tcp.local.") is already friendly.
        # For _raop, names look like "ABCDEF012345@WiiM Ultra-ABC1._raop..."
        # — strip everything up to the @.
        friendly = name.split(".")[0]
        if "@" in friendly:
            friendly = friendly.split("@", 1)[1]

        # Only treat this as a WiiM if either the service type is _linkplay
        # (definitive) or the AirPlay name starts with "WiiM" (heuristic).
        is_linkplay = type_.startswith("_linkplay.")
        looks_like_wiim = friendly.lower().startswith("wiim")
        if not is_linkplay and not looks_like_wiim:
            return

        source = "linkplay" if is_linkplay else "airplay"
        # Prefer linkplay info over airplay if we see both for the same IP.
        existing = self.devices.get(ip)
        if existing and existing["source"] == "linkplay":
            return
        self.devices[ip] = {"ip": ip, "name": friendly, "source": source}

    # ServiceListener requires these two methods, even if unused.
    def update_service(self, zc, type_, name):
        self.add_service(zc, type_, name)

    def remove_service(self, zc, type_, name):
        pass


def discover_wiim_devices(timeout: float = 2.5) -> list[dict]:
    """Browse the local network for WiiM devices. Returns list of
    {"ip", "name", "source"} dicts, sorted by name."""
    import time
    if not _ZEROCONF_AVAILABLE:
        raise RuntimeError(
            "zeroconf library not installed. install with: pip install zeroconf")

    zc = Zeroconf()
    listener = _WiimDiscoveryListener()
    browsers = []
    try:
        for svc_type in _MDNS_SERVICE_TYPES:
            try:
                browsers.append(ServiceBrowser(zc, svc_type, listener))
            except Exception:
                # Some service types may fail on weird network configs;
                # that's fine, we just skip them.
                pass
        time.sleep(timeout)
    finally:
        for b in browsers:
            try:
                b.cancel()
            except Exception:
                pass
        zc.close()

    return sorted(listener.devices.values(), key=lambda d: d["name"].lower())


# ──────────────────────────────────────────────────────────────────────────────
# API endpoints
# ──────────────────────────────────────────────────────────────────────────────

@app.get("/api/discover")
def api_discover():
    """Browse the LAN for WiiM devices via mDNS. Takes a few seconds."""
    if not _ZEROCONF_AVAILABLE:
        return jsonify({
            "ok": False,
            "error": "zeroconf library not installed on this server. "
                     "run 'pip install zeroconf' and restart, or enter the "
                     "IP manually.",
        }), 200
    try:
        timeout = float(request.args.get("timeout", "2.5"))
        timeout = max(0.5, min(timeout, 10.0))  # clamp 0.5–10s
    except ValueError:
        timeout = 2.5

    try:
        devices = discover_wiim_devices(timeout=timeout)
    except Exception as e:  # noqa: BLE001
        return jsonify({"ok": False, "error": str(e)}), 200
    return jsonify({"ok": True, "count": len(devices), "devices": devices})


@app.get("/api/test-connection")
def api_test_connection():
    """Verify a WiiM responds on the given IP by calling getStatusEx."""
    ip = (request.args.get("ip") or "").strip()
    use_http = request.args.get("http") == "1"
    if not ip:
        return jsonify({"ok": False, "error": "missing 'ip' parameter"}), 400

    client = WiimClient(ip, use_http=use_http, dry_run=False)
    try:
        info = client._call("getStatusEx")
    except requests.exceptions.SSLError:
        return jsonify({
            "ok": False,
            "error": "SSL error — try toggling 'Use plain HTTP' and retry.",
        }), 200
    except requests.exceptions.ConnectionError as e:
        return jsonify({
            "ok": False,
            "error": f"couldn't connect to {ip}: {e.__class__.__name__}. "
                     "check the IP and that the device is on the same network.",
        }), 200
    except requests.exceptions.Timeout:
        return jsonify({
            "ok": False,
            "error": f"timed out connecting to {ip}.",
        }), 200
    except Exception as e:  # noqa: BLE001 - surface any oddball failures to the UI
        return jsonify({"ok": False, "error": f"{e.__class__.__name__}: {e}"}), 200

    # getStatusEx returns a JSON object. The useful bits:
    name = (info or {}).get("ssid") or "(unknown)"
    firmware = (info or {}).get("firmware") or "(unknown)"
    project = (info or {}).get("project") or ""
    return jsonify({
        "ok": True,
        "device_name": name,
        "firmware": firmware,
        "project": project,
    })


@app.get("/api/get-current-eq")
def api_get_current_eq():
    """Read the active EQ bands currently loaded on the device for a given source."""
    ip = (request.args.get("ip") or "").strip()
    source = (request.args.get("source") or "wifi").strip()
    use_http = request.args.get("http") == "1"
    if not ip:
        return jsonify({"ok": False, "error": "missing 'ip' parameter"}), 400
    if source not in VALID_SOURCES:
        return jsonify({"ok": False, "error": f"invalid source '{source}'"}), 400
    client = WiimClient(ip, use_http=use_http)
    try:
        bands = client.get_current_eq(source)
    except Exception as e:  # noqa: BLE001
        return jsonify({"ok": False, "error": f"{e.__class__.__name__}: {e}"}), 502
    if bands is None:
        return jsonify({"ok": False, "error": "device returned no EQ data"}), 200
    return jsonify({
        "ok": True,
        "source": source,
        "bands": [{"type": b.type, "fc": b.fc, "gain": b.gain, "q": b.q}
                  for b in bands],
    })


@app.get("/api/headphones")
def api_headphones():
    """Return the full AutoEQ headphone list (cached after first call)."""
    try:
        items = load_headphone_index()
    except Exception as e:  # noqa: BLE001
        return jsonify({"ok": False, "error": str(e)}), 502
    return jsonify({"ok": True, "count": len(items), "headphones": items})


@app.get("/api/preview-peq")
def api_preview_peq():
    """Fetch and parse an AutoEQ profile without writing to the device.
    Returns the bands with both raw and preamp-adjusted gains for the editor."""
    folder_path = (request.args.get("path") or "").strip()
    preamp_mode = (request.args.get("preamp_mode") or "subtract").strip()
    if not folder_path:
        return jsonify({"ok": False, "error": "missing 'path' parameter"}), 400
    if preamp_mode not in {"subtract", "warn", "ignore"}:
        return jsonify({"ok": False, "error": "invalid preamp_mode"}), 400
    try:
        text, url = fetch_profile_from_path(folder_path)
        profile = parse_profile(text)
    except Exception as e:  # noqa: BLE001
        return jsonify({"ok": False, "error": f"couldn't load profile: {e}"}), 502
    adjust = profile.preamp if preamp_mode == "subtract" else 0.0
    return jsonify({
        "ok": True,
        "source_url": url,
        "preamp": profile.preamp,
        "preamp_applied": adjust,
        "bands": [
            {
                "type": b.type,
                "fc": round(b.fc, 2),
                "gain": round(b.gain, 2),
                "gain_device": round(b.gain + adjust, 2),
                "q": round(b.q, 3),
            }
            for b in profile.bands
        ],
    })


@app.post("/api/apply-peq")
def api_apply_peq():
    """Push a PEQ profile to the WiiM.

    Accepts two forms:
      • {"path": "...", "preamp_mode": "...", ...}  — fetch from AutoEQ then push
      • {"bands": [...], "name": "...", ...}         — push pre-parsed/edited bands directly
    """
    data = request.get_json(silent=True) or {}
    ip = (data.get("ip") or "").strip()
    source = (data.get("source") or "wifi").strip()
    use_http = bool(data.get("http"))

    if not ip:
        return jsonify({"ok": False, "error": "missing WiiM IP"}), 400
    if source not in VALID_SOURCES:
        return jsonify({"ok": False, "error": f"invalid source '{source}'"}), 400

    # ── Direct bands mode (from the editor) ──────────────────────────
    if data.get("bands") is not None:
        try:
            bands = [
                PeqBand(type=b["type"], fc=float(b["fc"]),
                        gain=float(b["gain"]), q=float(b["q"]))
                for b in data["bands"]
            ]
        except (KeyError, TypeError, ValueError) as e:
            return jsonify({"ok": False, "error": f"invalid bands: {e}"}), 400
        preset_name = (data.get("name") or "custom").strip()
        client = WiimClient(ip, use_http=use_http, dry_run=False)
        try:
            client.peq_off(source)
            for i, band in enumerate(bands):
                client.set_band(source, i, band)
            client.clear_unused_bands(source, used=len(bands))
            client.peq_save_name(source, preset_name)
            client.peq_on(source)
        except Exception as e:  # noqa: BLE001
            traceback.print_exc()
            return jsonify({
                "ok": False,
                "error": f"device call failed: {e.__class__.__name__}: {e}",
            }), 502
        return jsonify({
            "ok": True,
            "source_url": None,
            "preamp": 0.0,
            "preamp_applied": 0.0,
            "bands": [{"type": b.type, "fc": b.fc, "gain": b.gain, "q": b.q}
                      for b in bands],
        })

    # ── Path-based mode (fetch from AutoEQ) ──────────────────────────
    folder_path = (data.get("path") or "").strip()
    preamp_mode = data.get("preamp_mode") or "subtract"
    if not folder_path:
        return jsonify({"ok": False, "error": "missing headphone path"}), 400
    if preamp_mode not in {"subtract", "warn", "ignore"}:
        return jsonify({"ok": False, "error": "invalid preamp_mode"}), 400

    try:
        text, url = fetch_profile_from_path(folder_path)
        profile = parse_profile(text)
    except Exception as e:  # noqa: BLE001
        return jsonify({"ok": False, "error": f"couldn't load profile: {e}"}), 502

    adjust = profile.preamp if preamp_mode == "subtract" else 0.0

    client = WiimClient(ip, use_http=use_http, dry_run=False)
    try:
        client.peq_off(source)
        for i, band in enumerate(profile.bands):
            client.set_band(source, i, band, preamp_adjust=adjust)
        client.clear_unused_bands(source, used=len(profile.bands))
        client.peq_on(source)
    except Exception as e:  # noqa: BLE001
        traceback.print_exc()
        return jsonify({
            "ok": False,
            "error": f"device call failed: {e.__class__.__name__}: {e}",
        }), 502

    return jsonify({
        "ok": True,
        "source_url": url,
        "preamp": profile.preamp,
        "preamp_applied": adjust,
        "bands": [
            {"type": b.type, "fc": b.fc, "gain": b.gain, "q": b.q}
            for b in profile.bands
        ],
    })


@app.post("/api/peq-off")
def api_peq_off():
    data = request.get_json(silent=True) or {}
    ip = (data.get("ip") or "").strip()
    source = (data.get("source") or "wifi").strip()
    use_http = bool(data.get("http"))
    if not ip:
        return jsonify({"ok": False, "error": "missing WiiM IP"}), 400
    if source not in VALID_SOURCES:
        return jsonify({"ok": False, "error": f"invalid source '{source}'"}), 400

    client = WiimClient(ip, use_http=use_http, dry_run=False)
    try:
        client.peq_off(source)
    except Exception as e:  # noqa: BLE001
        return jsonify({"ok": False, "error": str(e)}), 502
    return jsonify({"ok": True})


# ──────────────────────────────────────────────────────────────────────────────
# Frontend — single-file HTML page
# ──────────────────────────────────────────────────────────────────────────────

INDEX_HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <title>WiiM × AutoEQ</title>
  <meta name="viewport" content="width=device-width,initial-scale=1">
  <style>
    :root {
      --bg: #0f1115;
      --panel: #171a21;
      --panel-2: #1e222b;
      --border: #2a2f3a;
      --text: #e6e8ee;
      --muted: #8b93a7;
      --accent: #6aa9ff;
      --accent-2: #4f86d8;
      --ok: #5cc98e;
      --err: #ff6b6b;
      --warn: #ffb84d;
    }
    @media (prefers-color-scheme: light) {
      :root {
        --bg: #f6f7fb; --panel: #ffffff; --panel-2: #f0f2f7;
        --border: #d9dde5; --text: #1a1d24; --muted: #5b6474;
        --accent: #2a6dd6; --accent-2: #1e54a8;
        --ok: #1f8a52; --err: #c23a3a; --warn: #b37408;
      }
    }
    * { box-sizing: border-box; }
    html, body { margin: 0; padding: 0; background: var(--bg); color: var(--text);
      font-family: ui-sans-serif, system-ui, -apple-system, "Segoe UI", Roboto,
        "Helvetica Neue", Arial, sans-serif;
      font-size: 15px; line-height: 1.45; }
    .wrap { max-width: 780px; margin: 0 auto; padding: 28px 20px 80px; }
    h1 { font-size: 22px; margin: 0 0 4px; letter-spacing: -0.01em; }
    .sub { color: var(--muted); font-size: 13px; margin-bottom: 20px; }
    .card { background: var(--panel); border: 1px solid var(--border);
      border-radius: 10px; padding: 18px 18px 16px; margin-bottom: 16px; }
    .card h2 { margin: 0 0 12px; font-size: 14px; color: var(--muted);
      font-weight: 600; text-transform: uppercase; letter-spacing: 0.06em; }
    label { display: block; font-size: 13px; color: var(--muted); margin-bottom: 6px; }
    input[type="text"], select {
      width: 100%; padding: 9px 11px; font-size: 14px;
      background: var(--panel-2); color: var(--text);
      border: 1px solid var(--border); border-radius: 7px;
      font-family: inherit;
    }
    input[type="text"]:focus, select:focus {
      outline: none; border-color: var(--accent);
      box-shadow: 0 0 0 2px rgba(106,169,255,0.15);
    }
    .row { display: flex; gap: 10px; align-items: flex-end; }
    .row > * { flex: 1; }
    .row > .shrink { flex: 0 0 auto; }
    button { cursor: pointer; font-family: inherit; font-size: 14px;
      border: 1px solid var(--border); background: var(--panel-2);
      color: var(--text); border-radius: 7px; padding: 9px 14px; }
    button:hover:not(:disabled) { border-color: var(--accent); }
    button:disabled { opacity: 0.55; cursor: not-allowed; }
    button.primary { background: var(--accent); color: white; border-color: var(--accent); }
    button.primary:hover:not(:disabled) { background: var(--accent-2); border-color: var(--accent-2); }
    button.danger { color: var(--err); border-color: rgba(255,107,107,0.35); }
    .status { margin-top: 10px; font-size: 13px; min-height: 1.2em; }
    .status.ok { color: var(--ok); }
    .status.err { color: var(--err); }
    .status.warn { color: var(--warn); }
    .muted { color: var(--muted); font-size: 12px; }
    .checkbox-row { display: flex; align-items: center; gap: 8px; margin-top: 10px; }
    .checkbox-row input { margin: 0; }
    .checkbox-row label { margin: 0; color: var(--text); font-size: 13px; }
    details { margin-top: 10px; }
    summary { cursor: pointer; color: var(--muted); font-size: 13px; user-select: none; }
    summary:hover { color: var(--text); }
    .band-table { width: 100%; border-collapse: collapse; margin-top: 10px;
      font-size: 13px; font-variant-numeric: tabular-nums; }
    .band-table th, .band-table td { padding: 6px 10px; text-align: left;
      border-bottom: 1px solid var(--border); }
    .band-table th { color: var(--muted); font-weight: 500; font-size: 12px;
      text-transform: uppercase; letter-spacing: 0.04em; }
    .badge { display: inline-block; padding: 2px 7px; border-radius: 4px;
      font-size: 11px; background: var(--panel-2); border: 1px solid var(--border);
      color: var(--muted); margin-left: 6px; }
    .caveats { font-size: 12px; color: var(--muted); border-top: 1px solid var(--border);
      padding-top: 14px; margin-top: 22px; line-height: 1.6; }
    .caveats strong { color: var(--text); }
    .hp-dropdown {
      position: relative;
    }
    .hp-list {
      position: absolute; top: 100%; left: 0; right: 0; z-index: 10;
      max-height: 260px; overflow-y: auto;
      background: var(--panel-2); border: 1px solid var(--border);
      border-top: none; border-radius: 0 0 7px 7px;
      display: none;
    }
    .hp-list.open { display: block; }
    .hp-item { padding: 7px 11px; cursor: pointer; font-size: 13px;
      border-bottom: 1px solid var(--border); }
    .hp-item:last-child { border-bottom: none; }
    .hp-item:hover, .hp-item.active { background: rgba(106,169,255,0.12); }
    .hp-item .sub-path { color: var(--muted); font-size: 11px; display: block; }
    .two-col { display: grid; grid-template-columns: 1fr 1fr; gap: 12px; }
    @media (max-width: 600px) { .two-col { grid-template-columns: 1fr; } }
    .eq-editor-table td { padding: 3px 6px; vertical-align: middle; }
    .eq-editor-table td:first-child { width: 28px; color: var(--muted); text-align: center; font-variant-numeric: tabular-nums; }
    .eq-editor-table input[type="number"], .eq-editor-table select {
      background: var(--panel-2); color: var(--text);
      border: 1px solid var(--border); border-radius: 4px;
      padding: 3px 6px; font-size: 13px; font-family: inherit;
      width: 100%; min-width: 0; box-sizing: border-box;
    }
    .eq-editor-table input[type="number"]:focus, .eq-editor-table select:focus {
      outline: none; border-color: var(--accent);
      box-shadow: 0 0 0 2px rgba(106,169,255,0.12);
    }
    .eq-editor-table .col-type { width: 74px; }
    .eq-editor-table .col-freq { width: 100px; }
    .eq-editor-table .col-gain { width: 90px; }
    .eq-editor-table .col-q   { width: 80px; }
  </style>
</head>
<body>
<div class="wrap">
  <h1>WiiM × AutoEQ</h1>
  <div class="sub">Push a headphone PEQ profile from AutoEQ to your WiiM over the local network.</div>

  <!-- ── WiiM connection ─────────────────────────────────────────── -->
  <div class="card">
    <h2>1. WiiM device</h2>

    <!-- Discovered devices dropdown (default view) -->
    <div id="discovered-view">
      <label for="device-select">Detected on your network</label>
      <div class="row">
        <div>
          <select id="device-select" disabled>
            <option value="">scanning network…</option>
          </select>
        </div>
        <div class="shrink">
          <button id="refresh-btn" title="Scan again">Refresh</button>
        </div>
        <div class="shrink">
          <button id="test-btn" class="primary">Test connection</button>
        </div>
      </div>
      <div class="muted" style="margin-top:8px;">
        Don't see your device?
        <a href="#" id="manual-toggle" style="color:var(--accent);">Enter IP manually</a>
      </div>
    </div>

    <!-- Manual IP entry (hidden by default) -->
    <div id="manual-view" style="display:none;">
      <label for="ip-manual">IP address</label>
      <div class="row">
        <div>
          <input type="text" id="ip-manual" placeholder="192.168.1.42" autocomplete="off">
        </div>
        <div class="shrink">
          <button id="test-btn-manual" class="primary">Test connection</button>
        </div>
      </div>
      <div class="muted" style="margin-top:8px;">
        <a href="#" id="discovered-toggle" style="color:var(--accent);">← back to auto-discover</a>
      </div>
    </div>

    <div class="checkbox-row">
      <input type="checkbox" id="use-http">
      <label for="use-http">Use plain HTTP (try this if HTTPS fails with a cert error)</label>
    </div>
    <div id="test-status" class="status"></div>
  </div>

  <!-- ── Headphone selection ─────────────────────────────────────── -->
  <div class="card">
    <h2>2. Headphone</h2>
    <label for="hp-search">Search the AutoEQ database (~6000 headphones)</label>
    <div class="hp-dropdown">
      <input type="text" id="hp-search" placeholder="loading list…" autocomplete="off" disabled>
      <div id="hp-list" class="hp-list"></div>
    </div>
    <div id="hp-selected" class="muted" style="margin-top:8px;"></div>
  </div>

  <!-- ── Options ─────────────────────────────────────────────────── -->
  <div class="card">
    <h2>3. Options</h2>
    <div class="two-col">
      <div>
        <label for="source">WiiM input source</label>
        <select id="source">
          <option value="wifi" selected>wifi (streaming)</option>
          <option value="line-in">line-in</option>
          <option value="bluetooth">bluetooth</option>
          <option value="optical">optical</option>
          <option value="coaxial">coaxial</option>
          <option value="hdmi">hdmi</option>
          <option value="phono">phono</option>
          <option value="usb">usb</option>
        </select>
      </div>
      <div>
        <label for="preamp-mode">Preamp handling</label>
        <select id="preamp-mode">
          <option value="subtract" selected>subtract from every band (recommended)</option>
          <option value="warn">warn only, don't modify gains</option>
          <option value="ignore">ignore the preamp entirely</option>
        </select>
      </div>
    </div>
    <div class="muted" style="margin-top: 8px;">
      WiiM has no preamp slider. AutoEQ profiles expect one — subtracting it keeps levels safe.
    </div>
  </div>

  <!-- ── Actions ─────────────────────────────────────────────────── -->
  <div class="card">
    <div class="row">
      <div class="shrink">
        <button id="apply-btn" class="primary" disabled>Load Profile</button>
      </div>
      <div class="shrink">
        <button id="off-btn" class="danger" disabled>Turn PEQ off</button>
      </div>
      <div></div>
    </div>
    <div id="apply-status" class="status"></div>
  </div>

  <!-- ── EQ band editor (hidden until profile loaded) ───────────── -->
  <div class="card" id="eq-editor" style="display:none;">
    <h2>4. EQ bands <span class="badge" style="text-transform:none;font-size:11px;">editable</span></h2>
    <div id="preamp-info" class="muted" style="margin-bottom:12px;font-size:13px;"></div>
    <div id="band-edit-container"></div>
    <div class="row" style="margin-top:14px;">
      <div class="shrink">
        <button id="confirm-apply-btn" class="primary">Apply to WiiM</button>
      </div>
      <div class="shrink">
        <button id="reset-bands-btn">Reset to original</button>
      </div>
      <div></div>
    </div>
    <div id="confirm-status" class="status"></div>
  </div>

  <div class="caveats">
    <strong>A few things to know.</strong>
    The PEQ endpoints this tool uses (<code>EQSetLV2SourceBand</code>, <code>EQChangeSourceFX</code>) are
    <em>not officially documented</em> — WiiM support has stated they're "closed". They work as of early 2026
    and are the same endpoints the open-source <a href="https://github.com/jeromeof/devicePEQ" target="_blank" rel="noopener">devicePEQ</a>
    uses, but a future firmware update could change that.
    After you click Apply, the WiiM Home app won't show the new values until you <strong>leave and re-enter</strong>
    the EQ screen — this is a known quirk, not a failure.
  </div>
</div>

<script>
  const $ = (id) => document.getElementById(id);
  const deviceSel = $("device-select");
  const refreshBtn = $("refresh-btn");
  const manualToggle = $("manual-toggle");
  const discoveredToggle = $("discovered-toggle");
  const discoveredView = $("discovered-view");
  const manualView = $("manual-view");
  const ipManual  = $("ip-manual");
  const httpCb    = $("use-http");
  const testBtn   = $("test-btn");
  const testBtnManual = $("test-btn-manual");
  const testStat  = $("test-status");
  const hpSearch  = $("hp-search");
  const hpList    = $("hp-list");
  const hpSel     = $("hp-selected");
  const sourceSel = $("source");
  const preampSel = $("preamp-mode");
  const applyBtn        = $("apply-btn");
  const offBtn          = $("off-btn");
  const applyStat       = $("apply-status");
  const eqEditor        = $("eq-editor");
  const preampInfo      = $("preamp-info");
  const bandEditCont    = $("band-edit-container");
  const confirmApplyBtn = $("confirm-apply-btn");
  const resetBandsBtn   = $("reset-bands-btn");
  const confirmStat     = $("confirm-status");

  let headphones = [];
  let selectedHp = null;      // { name, path }
  let connectionOk = false;
  let filterActive = 0;       // index of currently-highlighted dropdown item
  let mode = "discovered";    // "discovered" | "manual"
  let loadedProfile = null;   // response from /api/preview-peq

  function setStatus(el, text, cls) {
    el.textContent = text;
    el.className = "status" + (cls ? " " + cls : "");
  }

  function getCurrentIp() {
    return mode === "manual"
      ? ipManual.value.trim()
      : (deviceSel.value || "").trim();
  }

  function refreshApplyBtn() {
    const ip = getCurrentIp();
    const ready = connectionOk && selectedHp && ip;
    applyBtn.disabled = !ready;
    offBtn.disabled   = !(connectionOk && ip);
  }

  function invalidateConnection() {
    connectionOk = false;
    setStatus(testStat, "", "");
    refreshApplyBtn();
  }

  // ── Discovery / manual mode toggle ───────────────────────────────
  manualToggle.addEventListener("click", (e) => {
    e.preventDefault();
    mode = "manual";
    discoveredView.style.display = "none";
    manualView.style.display = "";
    invalidateConnection();
    ipManual.focus();
  });
  discoveredToggle.addEventListener("click", (e) => {
    e.preventDefault();
    mode = "discovered";
    discoveredView.style.display = "";
    manualView.style.display = "none";
    invalidateConnection();
  });

  deviceSel.addEventListener("change", invalidateConnection);
  ipManual.addEventListener("input", invalidateConnection);

  // ── Discover devices ─────────────────────────────────────────────
  async function discoverDevices() {
    deviceSel.innerHTML = '<option value="">scanning network…</option>';
    deviceSel.disabled = true;
    refreshBtn.disabled = true;
    testBtn.disabled = true;
    try {
      const r = await fetch("/api/discover");
      const j = await r.json();
      if (!j.ok) {
        deviceSel.innerHTML = '<option value="">(discovery unavailable)</option>';
        setStatus(testStat, "discovery error: " + (j.error || "unknown") +
          "  —  click \"Enter IP manually\" instead.", "warn");
        return;
      }
      if (!j.devices.length) {
        deviceSel.innerHTML = '<option value="">(no WiiM devices found)</option>';
        setStatus(testStat,
          "no devices detected. make sure your WiiM is on the same network, " +
          "or click \"Enter IP manually\".", "warn");
        return;
      }
      // Populate. Use IP as value, "Name — IP" as label.
      deviceSel.innerHTML = "";
      j.devices.forEach((d, i) => {
        const opt = document.createElement("option");
        opt.value = d.ip;
        opt.textContent = `${d.name}  —  ${d.ip}`;
        if (i === 0) opt.selected = true;
        deviceSel.appendChild(opt);
      });
      deviceSel.disabled = false;
      setStatus(testStat,
        `found ${j.devices.length} device${j.devices.length === 1 ? "" : "s"}. ` +
        `click Test connection to verify.`, "ok");
    } catch (e) {
      deviceSel.innerHTML = '<option value="">(discovery failed)</option>';
      setStatus(testStat, "discovery failed: " + e.message, "err");
    } finally {
      refreshBtn.disabled = false;
      testBtn.disabled = false;
    }
  }

  refreshBtn.addEventListener("click", discoverDevices);

  // ── Test connection ──────────────────────────────────────────────
  async function testConnection() {
    const ip = getCurrentIp();
    if (!ip) {
      setStatus(testStat, mode === "manual"
        ? "enter an IP first." : "no device selected.", "err");
      return;
    }
    setStatus(testStat, "contacting device…", "");
    testBtn.disabled = true;
    testBtnManual.disabled = true;
    try {
      const url = `/api/test-connection?ip=${encodeURIComponent(ip)}`
                + (httpCb.checked ? "&http=1" : "");
      const r = await fetch(url);
      const j = await r.json();
      if (j.ok) {
        connectionOk = true;
        setStatus(testStat,
          `✓ connected to "${j.device_name}" — firmware ${j.firmware}` +
          (j.project ? ` (${j.project})` : ""), "ok");
        loadDeviceEq();  // best-effort; silently ignored if device doesn't support it
      } else {
        connectionOk = false;
        setStatus(testStat, "✗ " + (j.error || "unknown error"), "err");
      }
    } catch (e) {
      connectionOk = false;
      setStatus(testStat, "✗ request failed: " + e.message, "err");
    } finally {
      testBtn.disabled = false;
      testBtnManual.disabled = false;
      refreshApplyBtn();
    }
  }
  testBtn.addEventListener("click", testConnection);
  testBtnManual.addEventListener("click", testConnection);

  // ── Load current EQ from device ──────────────────────────────────
  async function loadDeviceEq() {
    const ip = getCurrentIp();
    if (!ip) return;
    const url = `/api/get-current-eq?ip=${encodeURIComponent(ip)}`
              + `&source=${encodeURIComponent(sourceSel.value)}`
              + (httpCb.checked ? "&http=1" : "");
    let j;
    try {
      const r = await fetch(url);
      j = await r.json();
    } catch (_) {
      return;  // network error — silently skip
    }
    if (!j.ok || !j.bands || !j.bands.length) return;
    // Treat all gains as already "device-ready" (no preamp metadata available)
    const profile = {
      preamp: 0,
      preamp_applied: 0,
      bands: j.bands.map(b => ({ ...b, gain_device: b.gain })),
      _fromDevice: true,
    };
    loadedProfile = profile;
    renderBandEditor(profile);
    eqEditor.style.display = "";
    eqEditor.scrollIntoView({ behavior: "smooth", block: "nearest" });
  }


  // ── Headphone search ─────────────────────────────────────────────
  async function loadHeadphones() {
    try {
      const r = await fetch("/api/headphones");
      const j = await r.json();
      if (!j.ok) throw new Error(j.error || "failed to load");
      headphones = j.headphones;
      hpSearch.disabled = false;
      hpSearch.placeholder = `type to search (${j.count} headphones)…`;
    } catch (e) {
      hpSearch.placeholder = "couldn't load AutoEQ list";
      setStatus(testStat, "could not load headphone list: " + e.message, "err");
    }
  }

  function scorePath(path, measurementHints) {
    // mimic the CLI's preference: oratory1990 > Innerfidelity > Rtings > Headphone.com
    if (path.startsWith("oratory1990/"))   return 4;
    if (path.startsWith("Innerfidelity/")) return 3;
    if (path.startsWith("Rtings/"))        return 2;
    if (path.startsWith("Headphone.com"))  return 1;
    return 0;
  }

  function renderDropdown(items) {
    hpList.innerHTML = "";
    if (!items.length) {
      hpList.classList.remove("open");
      return;
    }
    items.forEach((hp, idx) => {
      const div = document.createElement("div");
      div.className = "hp-item" + (idx === filterActive ? " active" : "");
      div.innerHTML = `<div>${escapeHtml(hp.name)}</div>` +
                      `<span class="sub-path">${escapeHtml(hp.path)}</span>`;
      div.addEventListener("mousedown", (ev) => {
        ev.preventDefault();  // keep focus in input
        selectHeadphone(hp);
      });
      hpList.appendChild(div);
    });
    hpList.classList.add("open");
  }

  function escapeHtml(s) {
    return s.replace(/[&<>"']/g, c => ({
      "&":"&amp;","<":"&lt;",">":"&gt;","\"":"&quot;","'":"&#39;"
    }[c]));
  }

  function filterHeadphones(q) {
    if (!q) return [];
    const qLower = q.toLowerCase();
    // Match on name; rank by (measurement priority, shorter name first).
    const matched = headphones
      .filter(h => h.name.toLowerCase().includes(qLower))
      .map(h => ({ ...h, _score: scorePath(h.path) }))
      .sort((a, b) => b._score - a._score || a.name.length - b.name.length)
      .slice(0, 40);
    return matched;
  }

  hpSearch.addEventListener("input", () => {
    filterActive = 0;
    renderDropdown(filterHeadphones(hpSearch.value.trim()));
  });
  hpSearch.addEventListener("focus", () => {
    if (hpSearch.value.trim())
      renderDropdown(filterHeadphones(hpSearch.value.trim()));
  });
  hpSearch.addEventListener("blur", () => {
    // small timeout so click/mousedown handlers fire first
    setTimeout(() => hpList.classList.remove("open"), 150);
  });
  hpSearch.addEventListener("keydown", (e) => {
    const items = hpList.querySelectorAll(".hp-item");
    if (!items.length) return;
    if (e.key === "ArrowDown") {
      e.preventDefault();
      filterActive = Math.min(filterActive + 1, items.length - 1);
      renderDropdown(filterHeadphones(hpSearch.value.trim()));
    } else if (e.key === "ArrowUp") {
      e.preventDefault();
      filterActive = Math.max(filterActive - 1, 0);
      renderDropdown(filterHeadphones(hpSearch.value.trim()));
    } else if (e.key === "Enter") {
      e.preventDefault();
      const results = filterHeadphones(hpSearch.value.trim());
      if (results[filterActive]) selectHeadphone(results[filterActive]);
    } else if (e.key === "Escape") {
      hpList.classList.remove("open");
    }
  });

  function hideEditor() {
    eqEditor.style.display = "none";
    loadedProfile = null;
    setStatus(confirmStat, "", "");
  }

  function selectHeadphone(hp) {
    selectedHp = hp;
    hpSearch.value = hp.name;
    hpList.classList.remove("open");
    hpSel.innerHTML = `source: <code>${escapeHtml(hp.path)}</code>`;
    hideEditor();
    refreshApplyBtn();
  }

  // Hide editor if the user changes preamp mode after loading
  preampSel.addEventListener("change", hideEditor);

  // ── Load profile (step 1 of 2) ───────────────────────────────────
  applyBtn.addEventListener("click", async () => {
    if (!selectedHp) return;
    setStatus(applyStat, "loading profile from AutoEQ…", "");
    hideEditor();
    applyBtn.disabled = true;
    try {
      const url = `/api/preview-peq?path=${encodeURIComponent(selectedHp.path)}`
                + `&preamp_mode=${encodeURIComponent(preampSel.value)}`;
      const r = await fetch(url);
      const j = await r.json();
      if (!j.ok) {
        setStatus(applyStat, "✗ " + (j.error || "failed to load"), "err");
        return;
      }
      loadedProfile = j;
      const adjNote = j.preamp_applied !== 0
        ? ` (preamp ${j.preamp.toFixed(2)} dB already applied)`
        : "";
      setStatus(applyStat,
        `loaded ${j.bands.length} bands${adjNote} — review and edit below, then apply.`, "ok");
      renderBandEditor(j);
      eqEditor.style.display = "";
      eqEditor.scrollIntoView({ behavior: "smooth", block: "nearest" });
    } catch (e) {
      setStatus(applyStat, "✗ request failed: " + e.message, "err");
    } finally {
      refreshApplyBtn();
    }
  });

  // ── EQ band editor ───────────────────────────────────────────────
  function renderBandEditor(profile) {
    if (profile._fromDevice) {
      preampInfo.textContent =
        `Loaded from device (${sourceSel.value} source) — showing what's currently active.`;
    } else if (profile.preamp_applied !== 0) {
      preampInfo.textContent =
        `AutoEQ preamp: ${profile.preamp.toFixed(2)} dB — already subtracted from the gains below.`;
    } else if (profile.preamp !== 0) {
      preampInfo.textContent =
        `AutoEQ preamp: ${profile.preamp.toFixed(2)} dB — not applied (preamp mode setting).`;
    } else {
      preampInfo.textContent = "";
    }

    const bands = [...profile.bands].sort((a, b) => a.fc - b.fc);

    let html = `<table class="band-table eq-editor-table">
      <thead><tr>
        <th>#</th>
        <th class="col-type">Type</th>
        <th class="col-freq">Freq (Hz)</th>
        <th class="col-gain">Gain (dB)</th>
        <th class="col-q">Q</th>
      </tr></thead><tbody>`;
    bands.forEach((b, i) => {
      const gain = b.gain_device !== undefined ? b.gain_device : b.gain;
      html += `<tr data-band="${i}">
        <td>${i}</td>
        <td><select class="band-type">
          <option value="PK"${b.type === "PK" ? " selected" : ""}>PK</option>
          <option value="LSC"${b.type === "LSC" ? " selected" : ""}>LSC</option>
          <option value="HSC"${b.type === "HSC" ? " selected" : ""}>HSC</option>
        </select></td>
        <td><input type="number" class="band-freq" value="${b.fc}" step="1" min="20" max="20000"></td>
        <td><input type="number" class="band-gain" value="${gain}" step="0.1" min="-30" max="30"></td>
        <td><input type="number" class="band-q" value="${b.q}" step="0.01" min="0.1" max="10"></td>
      </tr>`;
    });
    html += `</tbody></table>`;
    bandEditCont.innerHTML = html;
  }

  function getBandsFromTable() {
    return Array.from(bandEditCont.querySelectorAll("tr[data-band]")).map(row => ({
      type: row.querySelector(".band-type").value,
      fc:   parseFloat(row.querySelector(".band-freq").value),
      gain: parseFloat(row.querySelector(".band-gain").value),
      q:    parseFloat(row.querySelector(".band-q").value),
    }));
  }

  // ── Confirm apply (step 2 of 2) ──────────────────────────────────
  confirmApplyBtn.addEventListener("click", async () => {
    const bands = getBandsFromTable();
    setStatus(confirmStat, "writing to device…", "");
    confirmApplyBtn.disabled = true;
    resetBandsBtn.disabled = true;
    try {
      const r = await fetch("/api/apply-peq", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          ip: getCurrentIp(),
          source: sourceSel.value,
          http: httpCb.checked,
          bands,
          name: selectedHp ? selectedHp.name : "custom",
        }),
      });
      const j = await r.json();
      if (!j.ok) {
        setStatus(confirmStat, "✗ " + (j.error || "failed"), "err");
      } else {
        setStatus(confirmStat,
          `✓ ${j.bands.length} bands written. exit & re-enter the EQ screen in WiiM Home to see them.`, "ok");
      }
    } catch (e) {
      setStatus(confirmStat, "✗ request failed: " + e.message, "err");
    } finally {
      confirmApplyBtn.disabled = false;
      resetBandsBtn.disabled = false;
    }
  });

  resetBandsBtn.addEventListener("click", () => {
    if (loadedProfile) renderBandEditor(loadedProfile);
    setStatus(confirmStat, "", "");
  });

  // ── Turn off ─────────────────────────────────────────────────────
  offBtn.addEventListener("click", async () => {
    setStatus(applyStat, "turning PEQ off on " + sourceSel.value + "…", "");
    applyBtn.disabled = true; offBtn.disabled = true;
    try {
      const r = await fetch("/api/peq-off", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          ip: getCurrentIp(),
          source: sourceSel.value,
          http: httpCb.checked,
        }),
      });
      const j = await r.json();
      if (j.ok) setStatus(applyStat, "✓ PEQ turned off on " + sourceSel.value + ".", "ok");
      else      setStatus(applyStat, "✗ " + (j.error || "failed"), "err");
    } catch (e) {
      setStatus(applyStat, "✗ request failed: " + e.message, "err");
    } finally {
      refreshApplyBtn();
    }
  });

  // ── Boot ─────────────────────────────────────────────────────────
  loadHeadphones();
  discoverDevices();
</script>
</body>
</html>
"""


@app.get("/")
def index():
    return render_template_string(INDEX_HTML)


# ──────────────────────────────────────────────────────────────────────────────
# Entry point
# ──────────────────────────────────────────────────────────────────────────────

def main() -> int:
    ap = argparse.ArgumentParser(description="Local web UI for wiim_autoeq.")
    ap.add_argument("--host", default="127.0.0.1",
                    help="host to bind (default: 127.0.0.1 — local machine only).")
    ap.add_argument("--port", type=int, default=5173,
                    help="port to serve on (default: 5173).")
    ap.add_argument("--debug", action="store_true",
                    help="enable Flask debug mode.")
    ap.add_argument("--log-level", default="WARNING",
                    choices=("DEBUG", "INFO", "WARNING", "ERROR"),
                    help="Logging verbosity (default: WARNING). Use DEBUG to "
                         "see every HTTP request and response sent to the WiiM.")
    args = ap.parse_args()

    logging.basicConfig(level=getattr(logging, args.log_level),
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
                        datefmt="%H:%M:%S")

    print(f"\n  WiiM × AutoEQ web UI")
    print(f"  ────────────────────")
    print(f"  open:  http://{args.host}:{args.port}/\n")
    app.run(host=args.host, port=args.port, debug=args.debug, use_reloader=False)
    return 0


if __name__ == "__main__":
    sys.exit(main())
