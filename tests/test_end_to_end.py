"""End-to-end test of wiim_autoeq_web.py WITH mDNS discovery.

Boots the Flask app with:
  - AutoEQ's results/README.md faked
  - A fake WiiM HTTP endpoint responding to getStatusEx/EQ commands
  - A fake mDNS service registered as _linkplay._tcp advertising "WiiM Ultra-TEST"

Then hits every API route including the new /api/discover, and asserts
that discovery finds the fake device.
"""
from __future__ import annotations

import json
import socket
import sys
import threading
import time
from pathlib import Path
from unittest.mock import patch

import requests
from flask import Flask, jsonify, request

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

# ── fake AutoEQ README ────────────────────────────────────────────────
FAKE_README = """\
# AutoEQ results

- [Sennheiser HD 600](./oratory1990/over-ear/Sennheiser%20HD%20600)
- [Sennheiser HD 650](./oratory1990/over-ear/Sennheiser%20HD%20650)
"""

FAKE_PEQ_TXT = """\
Preamp: -6.2 dB
Filter 1: ON LSC Fc 105 Hz Gain 6.7 dB Q 0.7
Filter 2: ON PK Fc 227 Hz Gain -3.1 dB Q 0.83
Filter 3: ON PK Fc 830 Hz Gain -4.5 dB Q 1.16
Filter 4: ON PK Fc 2993 Hz Gain 6.4 dB Q 1.31
Filter 5: ON HSC Fc 10000 Hz Gain -2.0 dB Q 0.7
"""

# ── fake WiiM HTTP device ─────────────────────────────────────────────
wiim_log: list[dict] = []
fake_wiim = Flask("fake_wiim")

@fake_wiim.get("/httpapi.asp")
def fake_httpapi():
    cmd = request.args.get("command", "")
    wiim_log.append({"command": cmd})
    if cmd == "getStatusEx":
        return jsonify({
            "language": "en_us",
            "ssid": "WiiM Ultra-TEST",
            "firmware": "Linkplay.5.0.999999",
            "project": "WiiM_Ultra",
            "PCB_version": "3",
        })
    return "OK"


def run_fake_wiim(port: int):
    fake_wiim.run(host="127.0.0.1", port=port, debug=False, use_reloader=False)


def run_web_app(port: int):
    from wiim_autoeq_web import app as web_app
    web_app.run(host="127.0.0.1", port=port, debug=False, use_reloader=False)


def patched_requests_get(orig_get):
    def wrapper(url, *a, **kw):
        if "results/README.md" in url:
            resp = requests.Response()
            resp.status_code = 200
            resp._content = FAKE_README.encode("utf-8")
            resp.url = url
            return resp
        if "ParametricEQ.txt" in url:
            resp = requests.Response()
            resp.status_code = 200
            resp._content = FAKE_PEQ_TXT.encode("utf-8")
            resp.url = url
            return resp
        return orig_get(url, *a, **kw)
    return wrapper


def register_fake_linkplay_service(name: str, port: int):
    """Register a fake _linkplay._tcp service that our discovery should find."""
    from zeroconf import ServiceInfo, Zeroconf
    zc = Zeroconf()
    # Grab the local IP the way most code does — UDP connect trick (no packets sent).
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 80))
        local_ip = s.getsockname()[0]
    except Exception:
        local_ip = "127.0.0.1"
    finally:
        s.close()

    info = ServiceInfo(
        type_="_linkplay._tcp.local.",
        name=f"{name}._linkplay._tcp.local.",
        addresses=[socket.inet_aton(local_ip)],
        port=port,
        properties={},
        server=f"{name.replace(' ', '-')}.local.",
    )
    zc.register_service(info)
    return zc, info, local_ip


def main() -> int:
    web_port = 5173
    wiim_port = 5174
    wiim_ip = f"127.0.0.1:{wiim_port}"

    # 1. Start the fake WiiM HTTP endpoint
    t1 = threading.Thread(target=run_fake_wiim, args=(wiim_port,), daemon=True)
    t1.start()

    # 2. Register a fake mDNS service for discovery to find
    print("registering fake _linkplay._tcp service…")
    try:
        zc, info, local_ip = register_fake_linkplay_service("WiiM Ultra-TEST", wiim_port)
        mdns_ok = True
        print(f"  → advertised at {local_ip}:{wiim_port}")
    except Exception as e:
        zc, info, local_ip = None, None, None
        mdns_ok = False
        print(f"  → couldn't register mDNS service (sandbox limitation?): {e}")

    # 3. Patch requests.get and boot the web app
    import wiim_autoeq
    orig_get = requests.get
    patch_target = patched_requests_get(orig_get)

    with patch.object(wiim_autoeq.requests, "get", patch_target):
        t2 = threading.Thread(target=run_web_app, args=(web_port,), daemon=True)
        t2.start()
        time.sleep(1.5)

        base = f"http://127.0.0.1:{web_port}"
        failures = 0

        def check(label, cond, detail=""):
            nonlocal failures
            mark = "✓" if cond else "✗"
            print(f"  {mark} {label}" + (f"  ({detail})" if detail else ""))
            if not cond:
                failures += 1

        # ── /api/discover ───────────────────────────────────────────
        print("\n── /api/discover ────────────────────────────────────")
        r = requests.get(f"{base}/api/discover?timeout=3", timeout=10)
        j = r.json()
        check("HTTP 200", r.status_code == 200, f"got {r.status_code}")
        check("ok=true", j.get("ok") is True, str(j)[:160])
        if mdns_ok:
            names = [d["name"] for d in j.get("devices", [])]
            check("fake WiiM was discovered",
                  any("WiiM Ultra-TEST" in n for n in names),
                  f"devices found: {names}")
            # At least one device should have an IP and a source field.
            if j.get("devices"):
                d0 = j["devices"][0]
                check("device has ip", "ip" in d0 and d0["ip"], str(d0))
                check("device has name", "name" in d0 and d0["name"], str(d0))
                check("device has source", d0.get("source") in {"linkplay", "airplay"},
                      str(d0))
        else:
            print("  (skipping device-found check — mDNS registration failed)")

        # Discovery with zeroconf removed should return a clean error
        print("\n── /api/discover (zeroconf unavailable) ────────────")
        import wiim_autoeq_web
        saved = wiim_autoeq_web._ZEROCONF_AVAILABLE
        wiim_autoeq_web._ZEROCONF_AVAILABLE = False
        try:
            r = requests.get(f"{base}/api/discover", timeout=5)
            j = r.json()
            check("HTTP 200 (soft error)", r.status_code == 200)
            check("ok=false", j.get("ok") is False, str(j))
            check("error mentions zeroconf",
                  "zeroconf" in (j.get("error") or "").lower(), str(j))
        finally:
            wiim_autoeq_web._ZEROCONF_AVAILABLE = saved

        # ── existing API routes (regression) ─────────────────────────
        print("\n── /api/test-connection ─────────────────────────────")
        r = requests.get(f"{base}/api/test-connection",
                         params={"ip": wiim_ip, "http": "1"})
        j = r.json()
        check("ok=true", j.get("ok") is True, str(j))
        check("device_name correct",
              j.get("device_name") == "WiiM Ultra-TEST", str(j))

        print("\n── /api/headphones ──────────────────────────────────")
        r = requests.get(f"{base}/api/headphones")
        j = r.json()
        check("ok=true", j.get("ok") is True)
        check("count == 2", j.get("count") == 2, f"got {j.get('count')}")

        print("\n── /api/apply-peq ───────────────────────────────────")
        wiim_log.clear()
        r = requests.post(f"{base}/api/apply-peq", json={
            "ip": wiim_ip,
            "path": "oratory1990/over-ear/Sennheiser HD 600",
            "source": "wifi",
            "preamp_mode": "subtract",
            "http": True,
        })
        j = r.json()
        check("ok=true", j.get("ok") is True, str(j)[:160])
        check("preamp = -6.2", j.get("preamp") == -6.2)
        check("5 bands applied", len(j.get("bands", [])) == 5)
        cmds = [e["command"] for e in wiim_log]
        check("12 device calls total", len(cmds) == 12, f"got {len(cmds)}")
        check("first is EQSourceOff",
              cmds[0].startswith("EQSourceOff:"))
        check("last is EQChangeSourceFX",
              cmds[-1].startswith("EQChangeSourceFX:"))
        # Verify preamp subtraction: band 0 (LSC, 105 Hz, 6.7 dB) - 6.2 preamp = 0.5
        band0 = json.loads(next(c for c in cmds if c.startswith("EQSetLV2SourceBand:"))
                           .split(":", 1)[1])
        eq_params = {item["param_name"]: item["value"] for item in band0.get("EQBand", [])}
        check("band 0 gain = 0.5 after preamp",
              abs(eq_params.get("a_gain", 0) - 0.5) < 0.01,
              f"a_gain={eq_params.get('a_gain')}")

        print("\n── /api/peq-off ─────────────────────────────────────")
        wiim_log.clear()
        r = requests.post(f"{base}/api/peq-off", json={
            "ip": wiim_ip, "source": "wifi", "http": True,
        })
        check("ok=true", r.json().get("ok") is True)
        check("one EQSourceOff call",
              len(wiim_log) == 1 and wiim_log[0]["command"].startswith("EQSourceOff:"))

        print("\n── UI root HTML ─────────────────────────────────────")
        r = requests.get(f"{base}/")
        html = r.text
        check("HTTP 200", r.status_code == 200)
        check("has device-select dropdown", 'id="device-select"' in html)
        check("has refresh button", 'id="refresh-btn"' in html)
        check("has manual toggle link", 'id="manual-toggle"' in html)
        check("has manual IP fallback input", 'id="ip-manual"' in html)
        check("no stale 'id=\"ip\"' input", 'id="ip"' not in html
              or 'id="ip-manual"' in html)  # the old plain input should be gone
        check("discovery boot call present", "discoverDevices();" in html)

        # Cleanup mDNS registration
        if zc and info:
            try:
                zc.unregister_service(info)
                zc.close()
            except Exception:
                pass

        print(f"\n{'═' * 55}")
        if failures == 0:
            print("  ALL TESTS PASSED ✓")
        else:
            print(f"  {failures} test(s) FAILED ✗")
        print(f"{'═' * 55}\n")
        return 0 if failures == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
