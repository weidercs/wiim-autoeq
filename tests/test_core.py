"""Unit tests for the shared core in src/wiim_autoeq.py."""
from __future__ import annotations

import json
import math
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import wiim_autoeq as w  # noqa: E402

PROFILE = """\
Preamp: -6.2 dB
Filter 1: ON LSC Fc 105 Hz Gain 6.7 dB Q 0.7
Filter 2: ON PK Fc 227 Hz Gain -3.1 dB Q 0.83
Filter 3: ON PK Fc 2993 Hz Gain 6.4 dB Q 1.31
"""


def bands(n: int) -> list[w.PeqBand]:
    return [w.PeqBand("PK", 100.0 * (i + 1), 1.0, 1.0) for i in range(n)]


# ── preamp planning ───────────────────────────────────────────────────

def test_shelf_mode_never_touches_band_gains():
    p = w.parse_profile(PROFILE)
    plan = w.plan_bands(p.bands, p.preamp, "shelf")
    assert [b.gain for b in plan.bands[:3]] == [6.7, -3.1, 6.4]
    shelf = plan.bands[3]
    assert (shelf.type, shelf.fc, shelf.gain) == ("HSC", w.PREAMP_SHELF_FC, -6.2)
    assert plan.preamp_applied == -6.2 and plan.warning is None


def test_subtract_is_alias_for_shelf():
    p = w.parse_profile(PROFILE)
    assert w.plan_bands(p.bands, p.preamp, "subtract") == w.plan_bands(p.bands, p.preamp, "shelf")


def test_full_profile_warns_instead_of_distorting():
    plan = w.plan_bands(bands(10), -5.0, "shelf")
    assert len(plan.bands) == 10 and plan.preamp_applied == 0
    assert all(b.gain == 1.0 for b in plan.bands)
    assert "volume" in plan.warning


@pytest.mark.parametrize("mode", ["warn", "ignore"])
def test_warn_and_ignore_write_profile_only(mode):
    plan = w.plan_bands(bands(3), -4.0, mode)
    assert len(plan.bands) == 3 and plan.preamp_applied == 0
    assert (plan.warning is not None) == (mode == "warn")


def test_zero_preamp_adds_no_band():
    assert len(w.plan_bands(bands(3), 0.0).bands) == 3


def test_preamp_band_round_trips_through_device_readback():
    plan = w.plan_bands(bands(4), -3.5)
    rest, preamp = w.split_preamp_band(plan.bands)
    assert rest == bands(4) and preamp == -3.5


def test_shelf_is_broadband_above_40hz():
    """The shelf must behave like a flat gain stage across the audible band."""
    def hs_db(f, fc=w.PREAMP_SHELF_FC, g=-6.0, q=w.PREAMP_SHELF_Q, fs=48000):
        import cmath
        A = 10 ** (g / 40); w0 = 2 * math.pi * fc / fs
        cw, sw = math.cos(w0), math.sin(w0); al = sw / (2 * q); s = math.sqrt(A)
        b = [A * ((A + 1) + (A - 1) * cw + 2 * s * al), -2 * A * ((A - 1) + (A + 1) * cw),
             A * ((A + 1) + (A - 1) * cw - 2 * s * al)]
        a = [(A + 1) - (A - 1) * cw + 2 * s * al, 2 * ((A - 1) - (A + 1) * cw),
             (A + 1) - (A - 1) * cw - 2 * s * al]
        z = cmath.exp(-1j * 2 * math.pi * f / fs)
        return 20 * math.log10(abs((b[0] + b[1] * z + b[2] * z * z) /
                                   (a[0] + a[1] * z + a[2] * z * z)))
    for f in (40, 100, 1000, 10000, 20000):
        assert abs(hs_db(f) - -6.0) < 0.5, f


# ── validation ────────────────────────────────────────────────────────

@pytest.mark.parametrize("bad", [
    w.PeqBand("BOGUS", 1000, 0, 1),
    w.PeqBand("PK", float("nan"), 0, 1),
    w.PeqBand("PK", 1000, float("inf"), 1),
    w.PeqBand("PK", 1000, 0, -1),
    w.PeqBand("PK", 1000, 99, 1),
    w.PeqBand("PK", 5, 0, 1),
])
def test_validate_rejects_bad_bands(bad):
    with pytest.raises(w.ProfileError):
        w.validate_bands([bad])


def test_validate_rejects_too_many_bands():
    with pytest.raises(w.ProfileError):
        w.validate_bands(bands(11))


def test_validate_normalises_type_case():
    assert w.validate_bands([w.PeqBand("pk", 1000, 0, 1)])[0].type == "PK"


@pytest.mark.parametrize("ok", ["192.168.1.42", "127.0.0.1:5174", "wiim.lan",
                                "wiim-ultra.local", "fe80::1", "[fe80::1]:443"])
def test_validate_host_accepts(ok):
    assert w.validate_host(ok) == ok


@pytest.mark.parametrize("bad", ["", "1.2.3.4/x", "h:8080/a#", "a@b", "evil.com?x",
                                 "host:0", "host:99999", "a b", "x:y:z"])
def test_validate_host_rejects(bad):
    with pytest.raises(ValueError):
        w.validate_host(bad)


# ── apply sequence ────────────────────────────────────────────────────

class Recorder(w.WiimClient):
    def __init__(self):
        super().__init__("192.0.2.1", use_http=True)
        self.calls: list[str] = []

    def _call(self, command):
        self.calls.append(command)
        return {"raw": "OK"}


def test_apply_plan_sequence():
    c = Recorder()
    c.apply_plan("wifi", w.plan_bands(bands(3), -2.0), "HD 600")
    verbs = [x.split(":", 1)[0] for x in c.calls]
    assert verbs == (["EQSourceOff"] + ["EQSetLV2SourceBand"] * 10
                     + ["EQSourceSave", "EQChangeSourceFX"])
    assert json.loads(c.calls[-2].split(":", 1)[1])["Name"] == "HD 600"


def test_unused_bands_are_disabled_not_flat():
    c = Recorder()
    c.apply_plan("wifi", w.plan_bands(bands(3)), "x")
    band_writes = [json.loads(x.split(":", 1)[1]) for x in c.calls
                   if x.startswith("EQSetLV2SourceBand:")]
    modes = [b["EQBand"][0]["value"] for b in band_writes]
    assert modes == [1, 1, 1] + [w.DISABLED_MODE] * 7


def test_apply_plan_validates_before_any_device_call():
    c = Recorder()
    plan = w.BandPlan([w.PeqBand("PK", 1000, 0, 1), w.PeqBand("XX", 1, 0, 1)], 0, 0)
    with pytest.raises(w.ProfileError):
        c.apply_plan("wifi", plan, "x")
    assert c.calls == []


def test_apply_plan_rejects_bad_source_before_any_call():
    c = Recorder()
    with pytest.raises(ValueError):
        c.apply_plan("nope", w.plan_bands(bands(1)), "x")
    assert c.calls == []


def test_client_encodes_spaces_as_percent20():
    c = w.WiimClient("192.0.2.1", use_http=True)
    with patch.object(c.session, "get") as get:
        get.return_value.json.return_value = {}
        c.peq_save_name("wifi", "Sennheiser HD 600")
    url = get.call_args[0][0]
    assert "Sennheiser%20HD%20600" in url and "+" not in url


def test_client_rejects_injected_host():
    with pytest.raises(ValueError):
        w.WiimClient("192.0.2.1/evil?")


# ── AutoEQ index ──────────────────────────────────────────────────────

README = """\
- [Sennheiser HD 600](./oratory1990/over-ear/Sennheiser%20HD%20600)
- [Sennheiser HD 600](./crinacle/GRAS/over-ear/Sennheiser%20HD%20600)
- [Sony WH-1000XM4 (2021)](./oratory1990/over-ear/Sony%20WH-1000XM4%20(2021))
- [Sneaky](./../../etc/passwd)
- [Readme](./README.md)
"""


def test_parse_index_handles_parens_and_skips_junk():
    idx = w.parse_autoeq_index(README)
    paths = [h["path"] for h in idx]
    assert "oratory1990/over-ear/Sony WH-1000XM4 (2021)" in paths
    assert not any(".." in p or p.endswith(".md") for p in paths)
    assert len(idx) == 3


def test_find_headphone_prefers_measurement():
    idx = w.parse_autoeq_index(README)
    assert w.find_headphone(idx, "HD 600")["path"].startswith("oratory1990/")
    assert w.find_headphone(idx, "HD 600", measurement="crinacle")["path"].startswith("crinacle/")


def test_find_headphone_miss_raises_lookuperror():
    with pytest.raises(LookupError):
        w.find_headphone(w.parse_autoeq_index(README), "AirPods Max")


def test_profile_url_rejects_traversal():
    with pytest.raises(w.ProfileError):
        w.profile_url("../../other/repo")


def test_preset_name_for_file():
    assert w.preset_name_for_file("/tmp/HD 600 ParametricEQ.txt") == "HD 600"
    assert w.preset_name_for_file("mine.txt") == "mine"


def test_device_name_prefers_devicename():
    assert w.device_name({"DeviceName": "Den", "ssid": "WiiM-1234"}) == "Den"
    assert w.device_name({"ssid": "WiiM-1234"}) == "WiiM-1234"
