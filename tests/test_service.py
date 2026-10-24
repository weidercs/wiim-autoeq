"""Tests for the wiim-eq service: devices, favorites, auto-per-input, API, MCP."""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import wiim_autoeq as core  # noqa: E402

svc = pytest.importorskip("wiim_eq_service")
import wiim_autoeq_web as web  # noqa: E402
import wiim_discovery as disc  # noqa: E402

INDEX = [
    {"name": "Sennheiser HD 600", "path": "oratory1990/over-ear/Sennheiser HD 600"},
    {"name": "Sennheiser HD 650", "path": "oratory1990/over-ear/Sennheiser HD 650"},
    {"name": "Sony WH-1000XM4", "path": "oratory1990/over-ear/Sony WH-1000XM4"},
]
PROFILE = "Preamp: -6.2 dB\nFilter 1: ON LSC Fc 105 Hz Gain 6.7 dB Q 0.7\n"
DEVICES = [
    {"name": "Office WiiM Ultra", "ip": "10.0.1.228"},
    {"name": "Theater WiiM Ultra", "ip": "10.0.1.170"},
    {"name": "Kitchen Speakers", "ip": "10.0.1.89"},
]


class FakeClient(core.WiimClient):
    calls: list[tuple[str, str]] = []

    def __init__(self, ip, **kw):
        super().__init__(ip, use_http=True)
        self.ip = ip

    def _call(self, command):
        FakeClient.calls.append((self.ip, command))
        return {"raw": "OK"}


@pytest.fixture
def env(tmp_path, monkeypatch):
    (tmp_path / "devices.json").write_text(json.dumps({"updated": time.time(),
                                                       "devices": DEVICES}))
    monkeypatch.setattr(svc, "DEVICES_FILE", tmp_path / "devices.json")
    monkeypatch.setattr(svc, "STATE_FILE", tmp_path / "state.json")
    monkeypatch.setattr(web, "load_headphone_index", lambda: INDEX)
    monkeypatch.setattr(core, "fetch_profile_from_path",
                        lambda p, timeout=20: (PROFILE, f"https://x/{p}"))
    monkeypatch.setattr(svc, "client_for", lambda d: FakeClient(d["ip"]))
    FakeClient.calls = []
    return tmp_path


def verbs(calls):
    return [c.split(":", 1)[0] for _, c in calls]


# ── sources / devices / headphones ────────────────────────────────────

@pytest.mark.parametrize("given,want", [
    ("WiFi", "wifi"), ("AirPlay", "wifi"), ("Line In", "line-in"), ("USB", "usb"),
    ("HDMI ARC", "hdmi"), ("TOSLINK", "optical"), (None, "wifi"), ("line_in", "line-in"),
    # Home Assistant LinkPlay source names
    ("Network", "wifi"), ("Optical In", "optical"), ("TV", "hdmi"), ("Phono In", "phono"),
])
def test_normalize_source(given, want):
    assert svc.normalize_source(given) == want


def test_normalize_source_rejects_unknown():
    with pytest.raises(ValueError):
        svc.normalize_source("cassette")


def test_resolve_device(env):
    assert svc.resolve_device("office")["ip"] == "10.0.1.228"
    assert svc.resolve_device("10.0.1.170")["name"] == "Theater WiiM Ultra"
    assert svc.resolve_device("10.0.1.99")["ip"] == "10.0.1.99"   # literal IP ok
    with pytest.raises(LookupError, match="several"):
        svc.resolve_device("ultra")
    with pytest.raises(LookupError):
        svc.resolve_device("garage")


def test_resolve_headphone_prefers_favorites(env):
    svc.add_favorite("My HD 650s", "oratory1990/over-ear/Sennheiser HD 650")
    assert svc.resolve_headphone("hd 650s")["path"].endswith("HD 650")
    assert svc.resolve_headphone("HD 600")["path"].endswith("HD 600")  # catalog search


# ── operations ────────────────────────────────────────────────────────

def test_apply_by_name(env):
    r = svc.apply_headphone("office", "HD 600", "USB")
    assert r["device"] == "Office WiiM Ultra" and r["source"] == "usb"
    assert r["preamp_applied"] == -6.2
    assert {ip for ip, _ in FakeClient.calls} == {"10.0.1.228"}
    assert verbs(FakeClient.calls)[0] == "EQSourceOff"
    assert '"source_name":"usb"' in FakeClient.calls[1][1]


def test_apply_bad_source_makes_no_calls(env):
    with pytest.raises(ValueError):
        svc.apply_headphone("office", "HD 600", "cassette")
    assert FakeClient.calls == []


def test_auto_rules_and_source_changed(env):
    svc.add_favorite("HD 600", "oratory1990/over-ear/Sennheiser HD 600")
    svc.set_auto("office", "USB", "HD 600")
    svc.set_auto("theater", "hdmi", "off")
    assert svc.get_auto() == {
        "Office WiiM Ultra": {"usb": "oratory1990/over-ear/Sennheiser HD 600"},
        "Theater WiiM Ultra": {"hdmi": "off"},
    }
    assert svc.source_changed("office", "usb")["action"] == "applied"
    FakeClient.calls = []
    assert svc.source_changed("theater", "HDMI")["action"] == "off"
    assert verbs(FakeClient.calls) == ["EQSourceOff"]
    FakeClient.calls = []
    assert svc.source_changed("office", "wifi")["action"] == "none"
    assert FakeClient.calls == []
    svc.set_auto("office", "usb", None)
    assert "Office WiiM Ultra" not in svc.get_auto()


def test_favorites_dedupe_and_remove(env):
    svc.add_favorite("A", "oratory1990/over-ear/Sennheiser HD 600")
    svc.add_favorite("B", "oratory1990/over-ear/Sennheiser HD 600")
    assert [f["name"] for f in svc.list_favorites()] == ["B"]
    assert svc.remove_favorite("B") == []


def test_favorite_rejects_bad_path(env):
    with pytest.raises(core.ProfileError):
        svc.add_favorite("x", "../../etc")


# ── HTTP API ──────────────────────────────────────────────────────────

def test_api_end_to_end(env):
    c = web.app.test_client()
    assert c.get("/api/v1/devices").get_json()["devices"][0]["name"] == "Kitchen Speakers"
    assert c.post("/api/v1/favorites", json={
        "name": "HD 600", "path": "oratory1990/over-ear/Sennheiser HD 600"}).status_code == 200
    assert c.get("/api/v1/favorites").get_json()["names"] == ["HD 600"]
    r = c.post("/api/v1/apply", json={"device": "office", "headphone": "HD 600",
                                      "source": "USB"})
    assert r.status_code == 200 and r.get_json()["headphone"] == "HD 600"
    r = c.post("/api/v1/apply", json={"device": "garage", "headphone": "HD 600"})
    assert r.status_code == 400 and "garage" in r.get_json()["error"]
    assert c.put("/api/v1/auto", json={"device": "office", "source": "usb",
                                       "headphone": "HD 600"}).status_code == 200
    assert c.post("/api/v1/source-changed", json={
        "device": "Office WiiM Ultra", "source": "USB"}).get_json()["action"] == "applied"
    assert c.post("/api/v1/off", json={"device": "office"}).get_json()["eq"] == "off"
    h = c.get("/api/v1/health").get_json()
    assert h["devices"] == 3 and h["discovery_age_s"] is not None


def test_ui_discover_uses_sidecar_file(env):
    j = web.app.test_client().get("/api/discover").get_json()
    assert j["ok"] and j["count"] == 3


# ── discovery sidecar ─────────────────────────────────────────────────

def test_discovery_merge_keeps_recent_and_forgets_stale(monkeypatch):
    monkeypatch.setattr(disc, "FORGET", 3600)
    prev = [{"name": "Gym", "ip": "1.1.1.1", "last_seen": 1000},
            {"name": "Old", "ip": "1.1.1.2", "last_seen": 0}]
    out = disc.merge(prev, [{"name": "Kitchen", "ip": "1.1.1.3"},
                            {"name": "Gym", "ip": "1.1.1.9"}], now=5000)
    assert [(d["name"], d["ip"]) for d in out] == [("Gym", "1.1.1.9"), ("Kitchen", "1.1.1.3")]


# ── MCP ───────────────────────────────────────────────────────────────

def test_mcp_tools_registered():
    pytest.importorskip("mcp")
    import asyncio
    tools = {t.name for t in asyncio.run(svc.build_mcp().list_tools())}
    assert {"list_wiim_devices", "apply_headphone_eq", "turn_eq_off",
            "set_auto_profile", "search_autoeq"} <= tools


def test_asgi_app_routes_mcp_and_ui(env):
    pytest.importorskip("mcp")
    from starlette.testclient import TestClient
    app = svc.build_app()
    with TestClient(app) as tc:
        assert tc.get("/api/v1/health").status_code == 200
        assert "WiiM" in tc.get("/").text
        r = tc.post("/mcp", headers={"Accept": "application/json, text/event-stream"},
                    json={"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
                        "protocolVersion": "2025-03-26", "capabilities": {},
                        "clientInfo": {"name": "t", "version": "1"}}})
        assert r.status_code == 200 and "wiim-eq" in r.text
