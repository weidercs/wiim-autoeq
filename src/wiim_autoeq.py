#!/usr/bin/env python3
"""
wiim_autoeq.py — load an AutoEQ headphone profile onto a WiiM Ultra (or Pro/Pro
Plus/Mini/Amp) over the local network.

Pulls a ParametricEQ.txt from jaakkopasanen/AutoEq by headphone name, parses it,
and writes it to the WiiM's per-source parametric EQ using the reverse-
engineered LinkPlay HTTP API.

IMPORTANT CAVEATS
-----------------
1. The PEQ HTTP endpoints (EQSetLV2SourceBand / EQChangeSourceFX) are NOT
   officially documented. WiiM support has stated they are "closed". They
   currently work (verified in devicePEQ, 2025) but could break in a future
   firmware update.
2. WiiM devices use a self-signed cert on HTTPS. This script disables TLS
   verification. You can also use --http to talk plain HTTP on port 80, which
   works on most firmwares.
3. WiiM's PEQ has no dedicated preamp. AutoEQ profiles assume a negative
   preamp to avoid clipping. Adding the preamp to each band's gain does NOT
   work for a parametric EQ (it reshapes the curve instead of lowering the
   level), so by default (--preamp-mode=shelf) the preamp is written to a
   spare band as a high shelf placed below the audible range, which acts as a
   broadband gain stage. If the profile uses all 10 bands there is no spare
   band; the preamp is then skipped with a warning and you should lower the
   device volume by that amount instead.

USAGE
-----
    # Push the HD 600 profile (oratory1990 / harman_over-ear_2018) to wifi source:
    python wiim_autoeq.py --ip 192.168.1.42 --headphone "Sennheiser HD 600"

    # Use a different source / measurement / target:
    python wiim_autoeq.py --ip 192.168.1.42 --headphone "HD 650" \\
        --source wifi --measurement oratory1990 --target harman_over-ear_2018

    # Dry run — print every URL we would hit, don't touch the device:
    python wiim_autoeq.py --ip 192.168.1.42 --headphone "HD 600" --dry-run

    # Load from a local .txt instead of fetching:
    python wiim_autoeq.py --ip 192.168.1.42 --file "./my headphone ParametricEQ.txt"

    # Turn PEQ off (restore original sound on the selected source):
    python wiim_autoeq.py --ip 192.168.1.42 --off
"""

from __future__ import annotations

import argparse
import ipaddress
import json
import logging
import math
import re
import sys
import urllib.parse
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

try:
    import requests
except ImportError:
    print("error: this script needs `requests`. install it with:\n"
          "    pip install requests", file=sys.stderr)
    sys.exit(2)

# Silence the self-signed cert warning — WiiM devices use self-signed certs.
import urllib3
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

logger = logging.getLogger(__name__)


# ──────────────────────────────────────────────────────────────────────────────
# Constants
# ──────────────────────────────────────────────────────────────────────────────

AUTOEQ_RESULTS_README = (
    "https://raw.githubusercontent.com/jaakkopasanen/AutoEq/master/results/README.md"
)
AUTOEQ_RAW_BASE = "https://raw.githubusercontent.com/jaakkopasanen/AutoEq/master"

# WiiM's internal PEQ plugin URI (reverse-engineered from devicePEQ).
PEQ_PLUGIN_URI = "http://moddevices.com/plugins/caps/EqNp"

# Each of the 10 PEQ bands is addressed by a letter: a=band 0 … j=band 9.
BAND_LETTERS = "abcdefghij"
N_BANDS = 10

# AutoEQ uses PK / LSC / HSC. WiiM's LV2 plugin uses integer type codes
# sent as the `{letter}_mode` param in EQSetLV2SourceBand payloads
# (verified in devicePEQ's wiimNetworkHandler.js).
#   1 = Peaking   (PK)
#   0 = Low shelf (LSC)
#   2 = High shelf (HSC)
FILTER_TYPE_CODE = {"PK": 1, "LSC": 0, "HSC": 2}

# WiiM source names (per their per-source EQ docs).
VALID_SOURCES = {"wifi", "line-in", "bluetooth", "optical", "coaxial",
                 "hdmi", "phono", "usb"}

PREAMP_MODES = ("shelf", "warn", "ignore")
# Accepted for backwards compatibility. The old "subtract" behaviour folded
# the preamp into every band's gain, which distorted the curve; it now maps
# to the correct shelf behaviour.
PREAMP_MODE_ALIASES = {"subtract": "shelf"}

# The broadband "preamp" band: a high shelf this low passes essentially the
# whole audio band at the shelf gain. Below ~40 Hz it ramps to half the gain
# at PREAMP_SHELF_FC, i.e. a slight relative bass lift in the bottom octave.
PREAMP_SHELF_TYPE = "HSC"
PREAMP_SHELF_FC = 20.0
PREAMP_SHELF_Q = 0.707

# Sanity limits for band values (match the web editor's input ranges).
FC_RANGE = (10.0, 24000.0)
GAIN_RANGE = (-30.0, 30.0)
Q_RANGE = (0.05, 20.0)


class ProfileError(ValueError):
    """Bad profile / band data. Raised before anything is sent to a device."""


# ──────────────────────────────────────────────────────────────────────────────
# AutoEQ profile parsing
# ──────────────────────────────────────────────────────────────────────────────

@dataclass
class PeqBand:
    type: str   # "PK" | "LSC" | "HSC"
    fc: float   # Hz
    gain: float # dB
    q: float


@dataclass
class Profile:
    preamp: float           # dB, usually negative
    bands: list[PeqBand]    # up to 10


FILTER_RE = re.compile(
    r"^Filter\s+\d+\s*:\s*ON\s+(PK|LSC|HSC)\s+Fc\s+([\d.]+)\s+Hz\s+"
    r"Gain\s+(-?[\d.]+)\s+dB\s+Q\s+([\d.]+)\s*$",
    re.IGNORECASE,
)
PREAMP_RE = re.compile(r"^Preamp\s*:\s*(-?[\d.]+)\s*dB\s*$", re.IGNORECASE)


def parse_profile(text: str) -> Profile:
    preamp = 0.0
    bands: list[PeqBand] = []
    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue
        m = PREAMP_RE.match(line)
        if m:
            preamp = float(m.group(1))
            continue
        m = FILTER_RE.match(line)
        if m:
            bands.append(PeqBand(
                type=m.group(1).upper(),
                fc=float(m.group(2)),
                gain=float(m.group(3)),
                q=float(m.group(4)),
            ))
    if not bands:
        raise ProfileError("no filters parsed — is this a ParametricEQ.txt file?")
    if len(bands) > N_BANDS:
        logger.warning("AutoEQ gave %d filters but WiiM only has %d PEQ bands; "
                       "keeping the first %d.", len(bands), N_BANDS, N_BANDS)
        bands = bands[:N_BANDS]
    return Profile(preamp=preamp, bands=bands)


def validate_bands(bands: list[PeqBand]) -> list[PeqBand]:
    """Check every band before anything touches the device. Returns the bands
    with normalised types; raises ProfileError on the first problem."""
    if len(bands) > N_BANDS:
        raise ProfileError(f"{len(bands)} bands given; WiiM supports {N_BANDS}")
    out: list[PeqBand] = []
    for i, b in enumerate(bands):
        btype = str(b.type).upper()
        if btype not in FILTER_TYPE_CODE:
            raise ProfileError(f"band {i}: unknown filter type {b.type!r} "
                               f"(expected one of {', '.join(FILTER_TYPE_CODE)})")
        for field, val, (lo, hi) in (("fc", b.fc, FC_RANGE),
                                     ("gain", b.gain, GAIN_RANGE),
                                     ("q", b.q, Q_RANGE)):
            if not isinstance(val, (int, float)) or not math.isfinite(val):
                raise ProfileError(f"band {i}: {field} must be a finite number")
            if not lo <= val <= hi:
                raise ProfileError(f"band {i}: {field}={val} outside {lo}–{hi}")
        out.append(PeqBand(btype, float(b.fc), float(b.gain), float(b.q)))
    return out


def normalize_preamp_mode(mode: str) -> str:
    mode = PREAMP_MODE_ALIASES.get(mode, mode)
    if mode not in PREAMP_MODES:
        raise ProfileError(f"invalid preamp mode {mode!r}")
    return mode


@dataclass
class BandPlan:
    bands: list[PeqBand]       # exactly what will be written, in order
    preamp: float              # preamp requested by the profile / user
    preamp_applied: float      # preamp actually realised on the device
    warning: Optional[str] = None


def preamp_band(preamp: float) -> PeqBand:
    return PeqBand(PREAMP_SHELF_TYPE, PREAMP_SHELF_FC, preamp, PREAMP_SHELF_Q)


def plan_bands(bands: list[PeqBand], preamp: float = 0.0,
               preamp_mode: str = "shelf") -> BandPlan:
    """Decide what to write. Band gains are never modified; the preamp is
    realised as a separate broadband shelf when a band is free."""
    mode = normalize_preamp_mode(preamp_mode)
    bands = validate_bands(bands)
    if not math.isfinite(preamp) or not GAIN_RANGE[0] <= preamp <= GAIN_RANGE[1]:
        raise ProfileError(f"preamp={preamp} outside {GAIN_RANGE[0]}–{GAIN_RANGE[1]}")

    if preamp == 0 or mode == "ignore":
        return BandPlan(bands, preamp, 0.0)
    manual = (f"profile wants {preamp:+.1f} dB preamp — lower the WiiM volume "
              f"by {abs(preamp):.1f} dB to avoid clipping.")
    if mode == "warn":
        return BandPlan(bands, preamp, 0.0, manual)
    if len(bands) >= N_BANDS:
        return BandPlan(bands, preamp, 0.0,
                        f"all {N_BANDS} bands are in use, so there is no spare "
                        f"band for the preamp. {manual}")
    return BandPlan(bands + [preamp_band(round(preamp, 2))], preamp, preamp)


def is_preamp_band(b: PeqBand) -> bool:
    return (b.type == PREAMP_SHELF_TYPE and abs(b.fc - PREAMP_SHELF_FC) < 0.01
            and abs(b.q - PREAMP_SHELF_Q) < 0.01)


def split_preamp_band(bands: list[PeqBand]) -> tuple[list[PeqBand], float]:
    """Inverse of plan_bands for EQ read back from a device: pull our preamp
    shelf (if present) out of the band list."""
    rest, preamp = [], 0.0
    for b in bands:
        if preamp == 0.0 and is_preamp_band(b):
            preamp = b.gain
        else:
            rest.append(b)
    return rest, preamp


def validate_host(host: str) -> str:
    """Accept an IPv4/IPv6 address or hostname, optionally with :port.
    Rejects anything that could smuggle a path/query into the device URL."""
    host = (host or "").strip()
    if not host:
        raise ValueError("missing device address")
    name, port = host, None
    if host.startswith("["):                      # [v6]:port
        end = host.find("]")
        if end < 0:
            raise ValueError(f"invalid address {host!r}")
        name, rest = host[1:end], host[end + 1:]
        if rest:
            if not rest.startswith(":"):
                raise ValueError(f"invalid address {host!r}")
            port = rest[1:]
        ipaddress.IPv6Address(name)
    elif host.count(":") == 1:
        name, port = host.split(":")
    if port is not None and not (port.isdigit() and 0 < int(port) < 65536):
        raise ValueError(f"invalid port in {host!r}")
    if not host.startswith("["):
        try:
            ipaddress.ip_address(name)
        except ValueError:
            if not re.fullmatch(r"(?=.{1,253}$)[A-Za-z0-9]([A-Za-z0-9-]{0,62}"
                                r"(\.[A-Za-z0-9-]{1,63})*)?\.?", name):
                raise ValueError(f"invalid address {host!r}") from None
            if host.count(":") > 1:
                raise ValueError(f"invalid address {host!r}")
    return host


# ──────────────────────────────────────────────────────────────────────────────
# AutoEQ index + fetching
# ──────────────────────────────────────────────────────────────────────────────

# Markdown links in AutoEQ's results README. The URL may contain balanced (…)
# from headphone names (e.g. "Sony WH-1000XM4 (2021)"), so allow one level of
# nested parens instead of a lazy match that stops at the first ')'.
_INDEX_LINK_RE = re.compile(r"\[([^\]]+)\]\(\.?/?((?:[^()\s]|\([^)]*\))*)\)")


def parse_autoeq_index(readme_text: str) -> list[dict]:
    """Parse AutoEQ's results/README.md into [{"name", "path"}], de-duplicated
    and sorted case-insensitively. `path` is relative to results/."""
    seen: set[tuple[str, str]] = set()
    items: list[dict] = []
    for name, path in _INDEX_LINK_RE.findall(readme_text):
        decoded = urllib.parse.unquote(path)
        if decoded.startswith("./"):
            decoded = decoded[2:]
        decoded = decoded.lstrip("/")
        if "/" not in decoded or decoded.endswith(".md") or ".." in decoded.split("/"):
            continue
        if (name, decoded) in seen:
            continue
        seen.add((name, decoded))
        items.append({"name": name, "path": decoded})
    items.sort(key=lambda d: d["name"].lower())
    return items


def fetch_autoeq_index(timeout: float = 20) -> list[dict]:
    r = requests.get(AUTOEQ_RESULTS_README, timeout=timeout)
    r.raise_for_status()
    return parse_autoeq_index(r.text)


def profile_url(folder_path: str) -> str:
    folder = folder_path.strip().rstrip("/")
    if not folder or ".." in folder.split("/"):
        raise ProfileError(f"invalid AutoEQ path {folder_path!r}")
    leaf = folder.rsplit("/", 1)[-1]
    file_path = f"{folder}/{leaf} ParametricEQ.txt"
    return f"{AUTOEQ_RAW_BASE}/results/{urllib.parse.quote(file_path, safe='/()')}"


def fetch_profile_from_path(folder_path: str, timeout: float = 20) -> tuple[str, str]:
    """Given an AutoEQ folder path like 'oratory1990/over-ear/Sennheiser HD 600',
    fetch the ParametricEQ.txt and return (text, url)."""
    url = profile_url(folder_path)
    r = requests.get(url, timeout=timeout)
    if r.status_code != 200:
        raise LookupError(
            f"AutoEQ returned HTTP {r.status_code} for {url} — the folder may "
            f"not contain a ParametricEQ.txt file.")
    return r.text, url


def find_headphone(index: list[dict], name: str,
                   measurement: str = "oratory1990",
                   target: str = "harman_over-ear_2018") -> dict:
    """Fuzzy-match a headphone name against the index, preferring the given
    measurement source and target. Raises LookupError if nothing matches."""
    wanted = name.lower().strip()
    candidates = [h for h in index if wanted in h["name"].lower()]
    if not candidates:
        raise LookupError(
            f"no AutoEQ profile found for '{name}'. try a simpler/partial "
            f"name (e.g. 'HD 600' instead of 'Sennheiser HD 600 v2').")

    def score(h: dict) -> float:
        s = 0.0
        if f"/{measurement}/" in f"/{h['path']}/":
            s += 10
        if target in h["path"]:
            s += 5
        return s - abs(len(h["name"]) - len(name)) * 0.1

    ranked = sorted(candidates, key=score, reverse=True)
    if len(ranked) > 1 and score(ranked[0]) == score(ranked[1]):
        logger.warning("multiple matches for %r — using %r. others: %s", name,
                       ranked[0]["name"], ", ".join(h["path"] for h in ranked[1:5]))
    return ranked[0]


def fetch_profile_by_name(
    name: str,
    measurement: str = "oratory1990",
    target: str = "harman_over-ear_2018",
) -> tuple[str, str, str]:
    """Returns (profile_text, resolved_url, display_name)."""
    hp = find_headphone(fetch_autoeq_index(timeout=15), name, measurement, target)
    text, url = fetch_profile_from_path(hp["path"], timeout=15)
    return text, url, hp["name"]


# ──────────────────────────────────────────────────────────────────────────────
# WiiM HTTP API client
# ──────────────────────────────────────────────────────────────────────────────

_CMD_SAFE = ':{}",'


class WiimClient:
    def __init__(self, ip: str, use_http: bool = False, dry_run: bool = False):
        scheme = "http" if use_http else "https"
        self.base = f"{scheme}://{validate_host(ip)}/httpapi.asp"
        self.dry_run = dry_run
        self.session = requests.Session()
        self.session.verify = False  # self-signed cert

    def _call(self, command: str) -> Optional[dict]:
        # quote() (not urlencode) so spaces become %20 rather than '+', which
        # LinkPlay firmware doesn't reliably decode back to a space.
        url = f"{self.base}?command={urllib.parse.quote(command, safe=_CMD_SAFE)}"
        if self.dry_run:
            print(f"  [dry-run] GET {url}")
            return None
        logger.debug("→ GET %s", url)
        try:
            r = self.session.get(url, timeout=10)
            r.raise_for_status()
        except requests.exceptions.RequestException as exc:
            logger.error("← %s: %s", type(exc).__name__, exc)
            raise
        logger.debug("← %d %s", r.status_code, r.text[:500])
        # Some endpoints return "OK"; others return JSON. Try JSON, fall back.
        try:
            return r.json()
        except ValueError:
            return {"raw": r.text.strip()}

    # High-level ops ──────────────────────────────────────────────────────────

    def _source_payload(self, source: str, **extra) -> str:
        if source not in VALID_SOURCES:
            raise ValueError(f"invalid source {source!r}")
        return json.dumps({"source_name": source, "pluginURI": PEQ_PLUGIN_URI,
                           **extra}, separators=(",", ":"))

    def status(self) -> dict:
        return self._call("getStatusEx") or {}

    def peq_off(self, source: str) -> None:
        self._call(f"EQSourceOff:{self._source_payload(source)}")

    def peq_on(self, source: str) -> None:
        self._call(f"EQChangeSourceFX:{self._source_payload(source)}")

    def set_band(self, source: str, index: int, band: PeqBand) -> None:
        """Writes one PEQ band. `index` is 0..9."""
        if not 0 <= index < N_BANDS:
            raise ValueError(f"band index out of range: {index}")
        letter = BAND_LETTERS[index]
        payload = {
            "pluginURI":   PEQ_PLUGIN_URI,
            "source_name": source,
            "EQBand": [
                {"param_name": f"{letter}_mode", "value": FILTER_TYPE_CODE[band.type]},
                {"param_name": f"{letter}_freq", "value": round(band.fc, 2)},
                {"param_name": f"{letter}_q",    "value": round(band.q, 3)},
                {"param_name": f"{letter}_gain", "value": round(band.gain, 2)},
            ],
            "EQStat":      "On",
            "channelMode": "Stereo",
        }
        blob = json.dumps(payload, separators=(",", ":"))
        self._call(f"EQSetLV2SourceBand:{blob}")

    def peq_save_name(self, source: str, name: str) -> None:
        self._call(f"EQSourceSave:{self._source_payload(source, Name=name)}")

    def get_current_eq(self, source: str) -> Optional[list[PeqBand]]:
        """Read the current EQ bands from the device for the given source.
        Returns a list of active (non-disabled) bands, or None if unavailable."""
        result = self._call(f"EQGetLV2SourceBandEx:{self._source_payload(source)}")
        if not result or "EQBand" not in result:
            return None
        # Response is a flat list of {param_name, value} entries, 4 per band:
        # {letter}_mode, {letter}_freq, {letter}_q, {letter}_gain
        # mode: -1=disabled, 0=LSC, 1=PK, 2=HSC
        _TYPE = {0: "LSC", 1: "PK", 2: "HSC"}
        bands: list[PeqBand] = []
        items = result["EQBand"]
        for i in range(0, len(items), 4):
            chunk = items[i:i + 4]
            if len(chunk) < 4:
                break
            params = {entry["param_name"].split("_", 1)[1]: entry["value"]
                      for entry in chunk}
            mode = int(params.get("mode", -1))
            if mode not in _TYPE:
                continue  # -1 = disabled band, skip
            bands.append(PeqBand(
                type=_TYPE[mode],
                fc=float(params.get("freq", 1000.0)),
                gain=float(params.get("gain", 0.0)),
                q=float(params.get("q", 1.0)),
            ))
        return bands

    def clear_unused_bands(self, source: str, used: int) -> None:
        """Zero out bands the profile didn't use, so stale settings don't bleed
        through from a previous profile."""
        flat = PeqBand(type="PK", fc=1000.0, gain=0.0, q=1.0)
        for i in range(used, N_BANDS):
            self.set_band(source, i, flat)

    def apply_plan(self, source: str, plan: BandPlan, name: str) -> None:
        """The one write sequence everything uses:
        EQSourceOff → bands → flatten the rest → EQSourceSave → EQChangeSourceFX.
        The plan is already validated, so nothing can fail on bad data mid-way."""
        if source not in VALID_SOURCES:
            raise ValueError(f"invalid source {source!r}")
        bands = validate_bands(plan.bands)
        self.peq_off(source)
        for i, band in enumerate(bands):
            self.set_band(source, i, band)
        self.clear_unused_bands(source, used=len(bands))
        self.peq_save_name(source, (name or "custom")[:64])
        self.peq_on(source)


def device_name(status: dict) -> str:
    return status.get("DeviceName") or status.get("ssid") or "(unknown)"


def preset_name_for_file(path: str) -> str:
    stem = Path(path).stem
    return re.sub(r"\s*ParametricEQ$", "", stem, flags=re.IGNORECASE) or stem


# ──────────────────────────────────────────────────────────────────────────────
# CLI
# ──────────────────────────────────────────────────────────────────────────────

def main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser(
        description="Push an AutoEQ headphone profile to a WiiM Ultra via its "
                    "local HTTP API.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    ap.add_argument("--ip", required=True, help="WiiM device IP address.")
    src = ap.add_mutually_exclusive_group(required=True)
    src.add_argument("--headphone", help="Headphone name, e.g. 'Sennheiser HD 600'.")
    src.add_argument("--file", help="Path to a local AutoEQ ParametricEQ.txt.")
    src.add_argument("--off", action="store_true",
                     help="Turn PEQ off on the chosen source and exit.")

    ap.add_argument("--source", default="wifi", choices=sorted(VALID_SOURCES),
                    help="WiiM input source to apply the PEQ to (default: wifi).")
    ap.add_argument("--measurement", default="oratory1990",
                    help="AutoEQ measurement source (default: oratory1990).")
    ap.add_argument("--target", default="harman_over-ear_2018",
                    help="AutoEQ target curve (default: harman_over-ear_2018).")

    ap.add_argument("--preamp-mode",
                    choices=PREAMP_MODES + tuple(PREAMP_MODE_ALIASES),
                    default="shelf",
                    help="How to handle the profile's preamp. 'shelf' writes it "
                         "to a spare band as a broadband gain stage (default). "
                         "'warn' just prints it. 'ignore' skips it. "
                         "('subtract' is a deprecated alias for 'shelf'.)")
    ap.add_argument("--http", action="store_true",
                    help="Use http:// instead of https://. Avoids self-signed "
                         "cert hassles; works on most WiiM firmwares.")
    ap.add_argument("--dry-run", action="store_true",
                    help="Print every URL instead of calling the device.")
    ap.add_argument("--log-level", default="WARNING",
                    choices=("DEBUG", "INFO", "WARNING", "ERROR"),
                    help="Logging verbosity (default: WARNING). Use DEBUG to "
                         "see every HTTP request and response sent to the WiiM.")

    args = ap.parse_args(argv)
    logging.basicConfig(level=getattr(logging, args.log_level),
                        format="%(levelname)s %(name)s: %(message)s")

    try:
        client = WiimClient(args.ip, use_http=args.http, dry_run=args.dry_run)
    except ValueError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2

    # --off: just disable PEQ on the source and exit.
    if args.off:
        print(f"turning PEQ off on source='{args.source}' …")
        client.peq_off(args.source)
        print("done.")
        return 0

    try:
        if args.file:
            with open(args.file, encoding="utf-8") as fh:
                text = fh.read()
            origin, preset_name = args.file, preset_name_for_file(args.file)
        else:
            text, origin, preset_name = fetch_profile_by_name(
                args.headphone, args.measurement, args.target)
            print(f"  → matched: {preset_name}", file=sys.stderr)
        profile = parse_profile(text)
        plan = plan_bands(profile.bands, profile.preamp, args.preamp_mode)
    except (LookupError, ProfileError, OSError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 1

    print(f"\nparsed profile from: {origin}")
    print(f"  preamp: {profile.preamp:+.2f} dB")
    print(f"  bands:  {len(profile.bands)}")
    for i, b in enumerate(profile.bands):
        print(f"    [{i}] {b.type}  Fc={b.fc:>7.1f} Hz  "
              f"Gain={b.gain:+6.2f} dB  Q={b.q:.3f}")
    if plan.preamp_applied:
        print(f"  → preamp {plan.preamp_applied:+.2f} dB written as a broadband "
              f"shelf on band {len(plan.bands) - 1}.")
    if plan.warning:
        print(f"  ! {plan.warning}")

    print(f"\nwriting to WiiM at {args.ip} (source='{args.source}') …")
    client.apply_plan(args.source, plan, preset_name)

    print("\ndone. open the WiiM Home app and navigate Device → EQ → "
          "Parametric EQ on the same source to verify.")
    print("(the app UI won't refresh until you leave and re-enter the EQ "
          "screen — this is a known quirk of the unofficial API.)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
