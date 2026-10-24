#!/usr/bin/env python3
"""
wiim_eq_service.py — the always-on "wiim-eq" service.

One process serving three front doors over the same core (wiim_autoeq.py):

  • the web UI (wiim_autoeq_web.py)            → /
  • a small JSON API for Home Assistant         → /api/v1/...
  • an MCP endpoint for Claude                  → /mcp   (streamable HTTP)

It adds what a long-running household service needs on top of the CLI/web UI:

  • device names instead of IPs — devices come from a JSON file written by the
    discovery sidecar (wiim_discovery.py), falling back to live mDNS
  • favorites — the handful of headphones you actually own
  • auto-per-input — "when the Office Ultra switches to USB, load the HD 600"
  • one write at a time per device (a lock), so HA, the UI and Claude can't
    interleave EQ writes

Configuration (environment):
  WIIM_DATA_DIR       where state.json lives (default: ./data)
  WIIM_DEVICES_FILE   devices.json written by the discovery sidecar
                      (default: $WIIM_DATA_DIR/devices.json)
  WIIM_USE_HTTP       "1" to talk plain HTTP to the devices (default: HTTPS)
  WIIM_PORT           listen port (default: 8000)
"""

from __future__ import annotations

import ipaddress
import json
import logging
import os
import sys
import tempfile
import threading
import time
from pathlib import Path
from typing import Optional

from flask import Blueprint, jsonify, request

sys.path.insert(0, str(Path(__file__).resolve().parent))

import wiim_autoeq as core  # noqa: E402
import wiim_autoeq_web as web  # noqa: E402

log = logging.getLogger("wiim_eq")

DATA_DIR = Path(os.environ.get("WIIM_DATA_DIR", "data"))
DEVICES_FILE = Path(os.environ.get("WIIM_DEVICES_FILE", DATA_DIR / "devices.json"))
STATE_FILE = DATA_DIR / "state.json"
USE_HTTP = os.environ.get("WIIM_USE_HTTP", "") in ("1", "true", "yes")


# ──────────────────────────────────────────────────────────────────────────────
# Sources — accept the names Home Assistant / the WiiM app show
# ──────────────────────────────────────────────────────────────────────────────

_SOURCE_ALIASES = {
    "wifi": "wifi", "wi-fi": "wifi", "network": "wifi", "airplay": "wifi",
    "spotify": "wifi", "tidal": "wifi", "dlna": "wifi", "streaming": "wifi",
    "line-in": "line-in", "line in": "line-in", "linein": "line-in",
    "aux": "line-in", "analog": "line-in", "analog in": "line-in",
    "bluetooth": "bluetooth", "bt": "bluetooth",
    "optical": "optical", "toslink": "optical", "optical in": "optical",
    "coaxial": "coaxial", "coax": "coaxial",
    "hdmi": "hdmi", "hdmi arc": "hdmi", "arc": "hdmi", "tv": "hdmi",
    "coaxial in": "coaxial", "coax in": "coaxial",
    "phono": "phono", "phono in": "phono",
    "usb": "usb", "usb dac": "usb", "usb audio": "usb", "usb in": "usb",
}


def normalize_source(source: Optional[str]) -> str:
    s = (source or "wifi").strip().lower().replace("_", " ")
    if s in _SOURCE_ALIASES:
        return _SOURCE_ALIASES[s]
    if s.replace(" ", "-") in core.VALID_SOURCES:
        return s.replace(" ", "-")
    raise ValueError(f"unknown source {source!r} (use one of "
                     f"{', '.join(sorted(core.VALID_SOURCES))})")


# ──────────────────────────────────────────────────────────────────────────────
# Persistent state (favorites + auto-per-input map)
# ──────────────────────────────────────────────────────────────────────────────

_state_lock = threading.RLock()


def _read_json(path: Path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return default
    except (OSError, ValueError) as e:
        log.error("couldn't read %s: %s", path, e)
        return default


def _write_json(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".tmp-", suffix=".json")
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=2, sort_keys=True)
    os.replace(tmp, path)


def load_state() -> dict:
    with _state_lock:
        st = _read_json(STATE_FILE, {})
    st.setdefault("favorites", [])
    st.setdefault("auto", {})
    return st


def save_state(st: dict) -> None:
    with _state_lock:
        _write_json(STATE_FILE, st)


def list_favorites() -> list[dict]:
    return sorted(load_state()["favorites"], key=lambda f: f["name"].lower())


def add_favorite(name: str, path: str) -> list[dict]:
    core.profile_url(path)  # validates the path shape
    name = (name or path.rsplit("/", 1)[-1]).strip()
    with _state_lock:
        st = load_state()
        st["favorites"] = [f for f in st["favorites"] if f["path"] != path]
        st["favorites"].append({"name": name, "path": path})
        save_state(st)
    return list_favorites()


def remove_favorite(key: str) -> list[dict]:
    with _state_lock:
        st = load_state()
        st["favorites"] = [f for f in st["favorites"]
                           if key not in (f["path"], f["name"])]
        save_state(st)
    return list_favorites()


def get_auto() -> dict:
    return load_state()["auto"]


def set_auto(device: str, source: str, headphone: Optional[str]) -> dict:
    """Map device+source → favorite path, "off", or None (remove)."""
    dev = resolve_device(device)["name"]
    src = normalize_source(source)
    if headphone not in (None, "", "off"):
        headphone = resolve_headphone(headphone)["path"]
    with _state_lock:
        st = load_state()
        per = st["auto"].setdefault(dev, {})
        if headphone in (None, ""):
            per.pop(src, None)
            if not per:
                st["auto"].pop(dev, None)
        else:
            per[src] = headphone
        save_state(st)
    return get_auto()


# ──────────────────────────────────────────────────────────────────────────────
# Devices
# ──────────────────────────────────────────────────────────────────────────────

def list_devices() -> list[dict]:
    """Devices from the sidecar's file; live mDNS if there's no file."""
    data = _read_json(DEVICES_FILE, None)
    if isinstance(data, dict) and isinstance(data.get("devices"), list):
        return sorted(data["devices"], key=lambda d: d["name"].lower())
    if web._ZEROCONF_AVAILABLE:
        return web.discover_wiim_devices(timeout=2.5)
    return []


def resolve_device(key: str) -> dict:
    """Find a device by exact name, IP, or a unique case-insensitive
    substring of its name ("office" → "Office WiiM Ultra")."""
    key = (key or "").strip()
    if not key:
        raise LookupError("no device given")
    devices = list_devices()
    for d in devices:
        if key == d["ip"] or key.lower() == d["name"].lower():
            return d
    hits = [d for d in devices if key.lower() in d["name"].lower()]
    if len(hits) == 1:
        return hits[0]
    if hits:
        raise LookupError(f"'{key}' matches several devices: "
                          f"{', '.join(d['name'] for d in hits)}")
    try:  # not discovered, but a literal IP (optionally :port) is fine
        core.validate_host(key)
        ipaddress.ip_address(key.rsplit(":", 1)[0] if key.count(":") == 1 else key.strip("[]"))
        return {"name": key, "ip": key, "source": "manual"}
    except ValueError:
        pass
    raise LookupError(f"no WiiM device matches '{key}'. known: "
                      f"{', '.join(d['name'] for d in devices) or '(none)'}")


_device_locks: dict[str, threading.Lock] = {}
_device_locks_guard = threading.Lock()


def _device_lock(ip: str) -> threading.Lock:
    with _device_locks_guard:
        return _device_locks.setdefault(ip, threading.Lock())


def client_for(device: dict) -> core.WiimClient:
    return core.WiimClient(device["ip"], use_http=USE_HTTP)


# ──────────────────────────────────────────────────────────────────────────────
# Headphones
# ──────────────────────────────────────────────────────────────────────────────

def resolve_headphone(query: str) -> dict:
    """Favorite name → AutoEQ path → fuzzy AutoEQ search, in that order."""
    q = (query or "").strip()
    if not q:
        raise LookupError("no headphone given")
    favs = list_favorites()
    for f in favs:
        if q.lower() in (f["name"].lower(), f["path"].lower()):
            return f
    fav_hits = [f for f in favs if q.lower() in f["name"].lower()]
    if len(fav_hits) == 1:
        return fav_hits[0]
    index = web.load_headphone_index()
    for h in index:
        if h["path"] == q:
            return h
    return core.find_headphone(index, q)


def search_headphones(query: str, limit: int = 15) -> list[dict]:
    q = (query or "").lower().strip()
    hits = [h for h in web.load_headphone_index() if q in h["name"].lower()]
    hits.sort(key=lambda h: (not h["path"].startswith("oratory1990/"), len(h["name"])))
    return hits[:limit]


# ──────────────────────────────────────────────────────────────────────────────
# Operations (shared by API + MCP)
# ──────────────────────────────────────────────────────────────────────────────

def apply_headphone(device: str, headphone: str, source: str = "wifi",
                    preamp_mode: str = "shelf") -> dict:
    dev = resolve_device(device)
    src = normalize_source(source)
    hp = resolve_headphone(headphone)
    text, url = core.fetch_profile_from_path(hp["path"])
    profile = core.parse_profile(text)
    plan = core.plan_bands(profile.bands, profile.preamp, preamp_mode)
    with _device_lock(dev["ip"]):
        client_for(dev).apply_plan(src, plan, hp["name"])
    log.info("applied %s to %s/%s", hp["name"], dev["name"], src)
    return {"ok": True, "device": dev["name"], "source": src,
            "headphone": hp["name"], "path": hp["path"], "source_url": url,
            "bands": len(profile.bands), "preamp": plan.preamp,
            "preamp_applied": plan.preamp_applied, "warning": plan.warning}


def eq_off(device: str, source: str = "wifi") -> dict:
    dev = resolve_device(device)
    src = normalize_source(source)
    with _device_lock(dev["ip"]):
        client_for(dev).peq_off(src)
    log.info("PEQ off on %s/%s", dev["name"], src)
    return {"ok": True, "device": dev["name"], "source": src, "eq": "off"}


def current_eq(device: str, source: str = "wifi") -> dict:
    dev = resolve_device(device)
    src = normalize_source(source)
    bands = client_for(dev).get_current_eq(src)
    if bands is None:
        return {"ok": False, "device": dev["name"], "source": src,
                "error": "device returned no EQ data"}
    bands, preamp = core.split_preamp_band(bands)
    return {"ok": True, "device": dev["name"], "source": src, "preamp": preamp,
            "bands": [web._band_dict(b) for b in bands]}


def source_changed(device: str, source: str) -> dict:
    """Called by HA when a device's input changes; applies the mapping if any."""
    dev = resolve_device(device)
    src = normalize_source(source)
    target = get_auto().get(dev["name"], {}).get(src)
    if not target:
        return {"ok": True, "device": dev["name"], "source": src, "action": "none"}
    if target == "off":
        return {**eq_off(dev["name"], src), "action": "off"}
    return {**apply_headphone(dev["name"], target, src), "action": "applied"}


# ──────────────────────────────────────────────────────────────────────────────
# JSON API for Home Assistant (and the UI's favorites / auto editor)
# ──────────────────────────────────────────────────────────────────────────────

api = Blueprint("wiim_eq_api", __name__, url_prefix="/api/v1")


def _run(fn, *args, **kw):
    try:
        return jsonify(fn(*args, **kw))
    except (LookupError, ValueError) as e:  # ProfileError is a ValueError
        return jsonify({"ok": False, "error": str(e)}), 400
    except Exception as e:  # noqa: BLE001 — device/network failures
        log.exception("request failed")
        return jsonify({"ok": False, "error": f"{e.__class__.__name__}: {e}"}), 502


def _body() -> dict:
    """JSON body, with query-string parameters as a fallback. The query form
    lets Home Assistant call the API from a shell_command with URL-encoded
    values (no JSON quoting to get wrong)."""
    return {**request.args.to_dict(), **(request.get_json(silent=True) or {})}


@api.get("/devices")
def api_devices():
    return _run(lambda: {"ok": True, "devices": list_devices()})


@api.get("/favorites")
def api_favorites():
    return _run(lambda: {"ok": True, "favorites": list_favorites(),
                         "names": [f["name"] for f in list_favorites()]})


@api.post("/favorites")
def api_add_favorite():
    b = _body()
    return _run(lambda: {"ok": True,
                         "favorites": add_favorite(b.get("name", ""), b.get("path", ""))})


@api.delete("/favorites")
def api_remove_favorite():
    b = _body()
    return _run(lambda: {"ok": True,
                         "favorites": remove_favorite(b.get("path") or b.get("name") or "")})


@api.post("/apply")
def api_apply():
    b = _body()
    return _run(apply_headphone, b.get("device", ""), b.get("headphone", ""),
                b.get("source") or "wifi", b.get("preamp_mode") or "shelf")


@api.post("/off")
def api_off():
    b = _body()
    return _run(eq_off, b.get("device", ""), b.get("source") or "wifi")


@api.get("/eq")
def api_eq():
    return _run(current_eq, request.args.get("device", ""),
                request.args.get("source") or "wifi")


@api.get("/auto")
def api_get_auto():
    return _run(lambda: {"ok": True, "auto": get_auto()})


@api.put("/auto")
def api_set_auto():
    b = _body()
    return _run(lambda: {"ok": True, "auto": set_auto(
        b.get("device", ""), b.get("source", ""), b.get("headphone"))})


@api.post("/source-changed")
def api_source_changed():
    b = _body()
    return _run(source_changed, b.get("device", ""), b.get("source", ""))


@api.get("/health")
def api_health():
    data = _read_json(DEVICES_FILE, {})
    age = time.time() - data.get("updated", 0) if isinstance(data, dict) else None
    return jsonify({"ok": True, "devices": len(list_devices()),
                    "discovery_age_s": round(age) if data else None})


web.app.register_blueprint(api)
# Let the UI's device dropdown use the sidecar's list instead of live mDNS
# (the main container isn't on the host network, so mDNS can't reach it).
web.DEVICE_LIST_PROVIDER = list_devices


# ──────────────────────────────────────────────────────────────────────────────
# MCP
# ──────────────────────────────────────────────────────────────────────────────

def build_mcp():
    from mcp.server.fastmcp import FastMCP
    from mcp.types import ToolAnnotations

    mcp = FastMCP(
        "wiim-eq",
        instructions=(
            "Controls the parametric EQ on the household's WiiM streamers using "
            "AutoEQ headphone profiles. Devices are referred to by name ('Office "
            "WiiM Ultra', or a unique part like 'office'). EQ is per input source "
            "(wifi, line-in, bluetooth, optical, coaxial, hdmi, phono, usb); all "
            "network streaming (AirPlay, Spotify, etc.) is 'wifi'. Prefer favorites "
            "— they are the headphones the household owns. Writing EQ changes "
            "what people are listening to: confirm with the user unless intent is "
            "unambiguous."),
        host="0.0.0.0", stateless_http=True)

    ro = ToolAnnotations(readOnlyHint=True, idempotentHint=True)
    rw = ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=True)

    @mcp.tool(annotations=ro)
    def list_wiim_devices() -> list[dict]:
        """WiiM devices on the network: name, IP."""
        return list_devices()

    @mcp.tool(annotations=ro)
    def list_favorite_headphones() -> list[dict]:
        """The household's favorite headphones (name + AutoEQ path)."""
        return list_favorites()

    @mcp.tool(annotations=ro)
    def search_autoeq(query: str, limit: int = 15) -> list[dict]:
        """Search the AutoEQ catalog (~6,000 headphones) by name."""
        return search_headphones(query, max(1, min(limit, 50)))

    @mcp.tool(annotations=ro)
    def get_eq(device: str, source: str = "wifi") -> dict:
        """Read the EQ bands currently active on a device's input source."""
        return current_eq(device, source)

    @mcp.tool(annotations=rw)
    def apply_headphone_eq(device: str, headphone: str, source: str = "wifi",
                           preamp_mode: str = "shelf") -> dict:
        """Load an AutoEQ profile onto a device's input source. `headphone` is a
        favorite name, an AutoEQ path, or a name to search for. preamp_mode:
        shelf (default) | warn | ignore."""
        return apply_headphone(device, headphone, source, preamp_mode)

    @mcp.tool(annotations=rw)
    def turn_eq_off(device: str, source: str = "wifi") -> dict:
        """Disable the parametric EQ on a device's input source."""
        return eq_off(device, source)

    @mcp.tool(annotations=rw)
    def add_favorite_headphone(path: str, name: str = "") -> list[dict]:
        """Star a headphone (by AutoEQ path from search_autoeq) as a favorite."""
        return add_favorite(name, path)

    @mcp.tool(annotations=rw)
    def remove_favorite_headphone(name_or_path: str) -> list[dict]:
        """Remove a favorite headphone."""
        return remove_favorite(name_or_path)

    @mcp.tool(annotations=ro)
    def get_auto_profiles() -> dict:
        """Auto-per-input map: device → source → headphone path (or 'off')."""
        return get_auto()

    @mcp.tool(annotations=rw)
    def set_auto_profile(device: str, source: str, headphone: str = "") -> dict:
        """When `device` switches to `source`, apply `headphone` automatically.
        Use 'off' to turn EQ off on that input, or '' to remove the rule."""
        return set_auto(device, source, headphone or None)

    return mcp


def build_app():
    """ASGI app: MCP at /mcp, everything else (UI + API) via the Flask app."""
    from a2wsgi import WSGIMiddleware

    asgi = build_mcp().streamable_http_app()
    asgi.mount("/", WSGIMiddleware(web.app))
    return asgi


def main() -> int:
    import uvicorn

    logging.basicConfig(level=os.environ.get("WIIM_LOG_LEVEL", "INFO"),
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    uvicorn.run(build_app(), host="0.0.0.0",
                port=int(os.environ.get("WIIM_PORT", "8000")),
                log_level="warning", proxy_headers=True, forwarded_allow_ips="*")
    return 0


if __name__ == "__main__":
    sys.exit(main())
