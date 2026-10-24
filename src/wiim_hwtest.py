#!/usr/bin/env python3
"""
wiim_hwtest.py — check that a WiiM still accepts what wiim-eq writes, then put
everything back exactly as it was.

Run inside the wiim-eq container (it uses the service's device list):

    docker exec -w /app wiim-eq python3 src/wiim_hwtest.py --device office --source phono

What it does on that one device input:
  1. reads the current EQ (bands, on/off state, preset name) — this is what
     gets restored. With --from-backup it restores the earliest
     /data/backups/eq-before-*.json entry for that device/input instead.
  2. TEST: writes those same bands plus a -1 dB preamp shelf on a spare band,
     with any remaining bands disabled (mode -1), and reads it back.
  3. RESTORE (always, even if the test fails): writes the original bands back,
     restores the preset name and on/off state, and compares.

Useful after a WiiM firmware update: if this fails, the unofficial PEQ API
changed. Exit code 0 = everything passed.
"""

from __future__ import annotations

import argparse
import glob
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import wiim_autoeq as c  # noqa: E402
import wiim_eq_service as s  # noqa: E402


def same(a: list[c.PeqBand], b: list[c.PeqBand]) -> bool:
    return len(a) == len(b) and all(
        x.type == y.type and abs(x.fc - y.fc) < 0.01 and abs(x.gain - y.gain) < 0.01
        and abs(x.q - y.q) < 0.01 for x, y in zip(a, b))


def probe_extra_slots(cl, src, raw, reported: int) -> bool:
    """For each slot past the documented 10, write a 0 dB peaking band with a
    distinctive frequency and check the device echoes mode AND frequency back.
    A slot that only *reports* -1 but ignores writes fails this."""
    letters = "abcdefghijklmnopqrstuvwxyz"
    extra = list(range(c.N_BANDS, reported))
    if not extra:
        print("PROBE: device reports no slots beyond", c.N_BANDS)
        return True
    results = {}
    for i in extra:
        letter, fc = letters[i], 1000.0 + i          # 1010, 1011, …
        payload = {"pluginURI": c.PEQ_PLUGIN_URI, "source_name": src,
                   "EQBand": [{"param_name": f"{letter}_mode", "value": 1},
                              {"param_name": f"{letter}_freq", "value": fc},
                              {"param_name": f"{letter}_q", "value": 1.0},
                              {"param_name": f"{letter}_gain", "value": 0.0}],
                   "EQStat": "On", "channelMode": "Stereo"}
        cl._call("EQSetLV2SourceBand:" + json.dumps(payload, separators=(",", ":")))
    cl.peq_on(src)
    time.sleep(1)
    r = raw()
    params = {e["param_name"]: e["value"] for e in r.get("EQBand", [])}
    for i in extra:
        letter, fc = letters[i], 1000.0 + i
        got_mode = int(params.get(f"{letter}_mode", -99))
        got_fc = float(params.get(f"{letter}_freq", -1))
        results[i] = (got_mode == 1 and abs(got_fc - fc) < 0.5)
        print(f"PROBE slot {i} ({letter}): wrote PK {fc:.0f} Hz → read mode={got_mode} "
              f"freq={got_fc:.0f} → {'REAL' if results[i] else 'IGNORED'}")
    print("PROBE: usable slots =", c.N_BANDS + sum(results.values()))
    return all(results.values())


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--device", required=True)
    ap.add_argument("--source", required=True)
    ap.add_argument("--from-backup", action="store_true",
                    help="restore the earliest eq-before-*.json backup instead of the current EQ")
    ap.add_argument("--probe-slots", action="store_true",
                    help="also write a harmless 0 dB band into every slot the device reports "
                         "beyond the first 10 and check it reads back (is the slot real?)")
    args = ap.parse_args()

    dev = s.resolve_device(args.device)
    src = s.normalize_source(args.source)
    cl = s.client_for(dev)

    def raw() -> dict:
        return cl._call("EQGetLV2SourceBandEx:" + cl._source_payload(src)) or {}

    def modes(r: dict) -> list[int]:
        return [int(e["value"]) for e in r.get("EQBand", []) if e["param_name"].endswith("_mode")]

    before = raw()
    stat, name = before.get("EQStat"), before.get("Name") or "restored"
    original = cl.get_current_eq(src) or []
    if args.from_backup:
        files = sorted(glob.glob("/data/backups/eq-before-*.json"))
        entry = json.loads(Path(files[0]).read_text())[dev["name"]][src] if files else None
        if not entry:
            print("no backup entry for", dev["name"], src)
            return 2
        original = [c.PeqBand(**b) for b in entry]
    print(f"{dev['name']} / {src}: EQStat={stat} Name={name!r} modes={modes(before)}")
    print(f"restore target: {len(original)} bands" + (" (from backup)" if args.from_backup else ""))
    if len(original) >= c.N_BANDS:
        print("all 10 bands in use — no spare band to test the shelf on; pick another input")
        return 2

    ok = True
    try:
        plan = c.plan_bands(original, -1.0, "shelf")
        cl.apply_plan(src, plan, "wiim-eq hw test")
        time.sleep(1)
        r, back = raw(), cl.get_current_eq(src) or []
        shelf = any(c.is_preamp_band(b) for b in back)
        # Every slot after the written bands must read back disabled. The
        # device may report more slots than we write (an Ultra on 5.2 reports
        # 12: a–l), so check the tail rather than counting.
        disabled = all(m == c.DISABLED_MODE for m in modes(r)[len(plan.bands):])
        print("TEST readback modes:", modes(r))
        print("SHELF PRESENT:", shelf, "| DISABLED ACCEPTED:", disabled)
        ok = shelf and disabled
        if args.probe_slots:
            ok = probe_extra_slots(cl, src, raw, len(modes(r))) and ok
    except Exception as e:  # noqa: BLE001
        print("TEST FAILED:", type(e).__name__, e)
        ok = False
    finally:
        cl.apply_plan(src, c.BandPlan(original, 0.0, 0.0), name)
        if str(stat).lower() == "off":
            cl.peq_off(src)
        time.sleep(1)
        r, now = raw(), cl.get_current_eq(src) or []
        restored = same(now, original) and r.get("Name") == name
        print("RESTORED identical:", restored, "| modes:", modes(r),
              "| EQStat:", r.get("EQStat"), "| Name:", r.get("Name"))
        ok = ok and restored
    print("RESULT:", "PASS" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
