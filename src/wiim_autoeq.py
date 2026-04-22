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
3. WiiM's PEQ has no dedicated preamp. AutoEQ profiles assume you apply a
   negative preamp to avoid clipping. This script subtracts the preamp from
   every band's gain by default (--preamp-mode=subtract). Use
   --preamp-mode=ignore to skip that, or --preamp-mode=warn to just print
   what preamp the profile wants you to set manually.

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
import json
import re
import sys
import urllib.parse
from dataclasses import dataclass
from typing import Iterable, Optional

try:
    import requests
except ImportError:
    print("error: this script needs `requests`. install it with:\n"
          "    pip install requests", file=sys.stderr)
    sys.exit(2)

# Silence the self-signed cert warning — WiiM devices use self-signed certs.
import urllib3
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)


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

# AutoEQ uses PK / LS / HS. WiiM's LV2 plugin uses integer type codes
# sent as the `{letter}_mode` param in EQSetLV2SourceBand payloads
# (verified in devicePEQ's wiimNetworkHandler.js).
#   0 = Peaking   (PK)
#   1 = Low shelf (LSC)
#   2 = High shelf (HSC)
# Newer firmwares also support HP/LP but AutoEQ never emits those.
FILTER_TYPE_CODE = {"PK": 0, "LSC": 1, "HSC": 2}

# WiiM source names (per their per-source EQ docs).
VALID_SOURCES = {"wifi", "line-in", "bluetooth", "optical", "coaxial",
                 "hdmi", "phono", "usb"}


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
        raise ValueError("no filters parsed — is this a ParametricEQ.txt file?")
    if len(bands) > N_BANDS:
        print(f"warning: AutoEQ gave {len(bands)} filters but WiiM only has "
              f"{N_BANDS} PEQ bands. keeping the first {N_BANDS}.",
              file=sys.stderr)
        bands = bands[:N_BANDS]
    return Profile(preamp=preamp, bands=bands)


# ──────────────────────────────────────────────────────────────────────────────
# Fetching from AutoEQ
# ──────────────────────────────────────────────────────────────────────────────

def fetch_profile_by_name(
    name: str,
    measurement: str = "oratory1990",
    target: str = "harman_over-ear_2018",
) -> tuple[str, str]:
    """Returns (profile_text, resolved_url). Does a fuzzy name match against
    the AutoEQ results README index."""
    readme = requests.get(AUTOEQ_RESULTS_README, timeout=15)
    readme.raise_for_status()
    body = readme.text

    # Match markdown links of the form:  [Display Name](./path/to/folder)
    link_re = re.compile(r"\[([^\]]+)\]\(\.?/?(\S+?)\)")
    candidates = []
    wanted = name.lower().strip()
    for disp, path in link_re.findall(body):
        # Unescape URL-encoded spaces and strip leading ./
        decoded = urllib.parse.unquote(path).lstrip("./")
        # We only care about ParametricEQ-bearing result folders.
        if "/" not in decoded:
            continue
        if wanted in disp.lower():
            candidates.append((disp, decoded))

    if not candidates:
        raise SystemExit(
            f"no AutoEQ profile found for '{name}'. try a simpler/partial "
            f"name (e.g. 'HD 600' instead of 'Sennheiser HD 600 v2').")

    # Prefer matches under the requested measurement/target.
    scored = []
    for disp, path in candidates:
        score = 0
        if f"/{measurement}/" in f"/{path}/":
            score += 10
        if target in path:
            score += 5
        # Prefer shorter/more-exact display names.
        score -= abs(len(disp) - len(name)) * 0.1
        scored.append((score, disp, path))
    scored.sort(reverse=True)

    if len(scored) > 1 and scored[0][0] == scored[1][0]:
        print("multiple matches — using the first. candidates:", file=sys.stderr)
        for s, disp, path in scored[:5]:
            print(f"  [{s:+.1f}] {disp}  ({path})", file=sys.stderr)

    _, disp, folder = scored[0]
    # Folder name is the last path segment; AutoEQ names the PEQ file after it.
    leaf = folder.rstrip("/").rsplit("/", 1)[-1]
    file_path = f"{folder.rstrip('/')}/{leaf} ParametricEQ.txt"
    url = f"{AUTOEQ_RAW_BASE}/results/{urllib.parse.quote(file_path)}"

    print(f"  → matched: {disp}", file=sys.stderr)
    print(f"  → fetching: {url}", file=sys.stderr)

    r = requests.get(url, timeout=15)
    if r.status_code != 200:
        raise SystemExit(
            f"AutoEQ returned HTTP {r.status_code} for {url}\n"
            f"the match's folder may not contain a ParametricEQ.txt — "
            f"try --measurement / --target to pick a different one.")
    return r.text, url


# ──────────────────────────────────────────────────────────────────────────────
# WiiM HTTP API client
# ──────────────────────────────────────────────────────────────────────────────

class WiimClient:
    def __init__(self, ip: str, use_http: bool = False, dry_run: bool = False):
        scheme = "http" if use_http else "https"
        self.base = f"{scheme}://{ip}/httpapi.asp"
        self.dry_run = dry_run
        self.session = requests.Session()
        self.session.verify = False  # self-signed cert

    def _call(self, command: str) -> Optional[dict]:
        params = {"command": command}
        url = f"{self.base}?{urllib.parse.urlencode(params, safe=':{}\",')}"
        if self.dry_run:
            print(f"  [dry-run] GET {url}")
            return None
        r = self.session.get(url, timeout=10)
        r.raise_for_status()
        # Some endpoints return "OK"; others return JSON. Try JSON, fall back.
        try:
            return r.json()
        except ValueError:
            return {"raw": r.text.strip()}

    # High-level ops ──────────────────────────────────────────────────────────

    def peq_off(self, source: str) -> None:
        payload = json.dumps({"source_name": source, "pluginURI": PEQ_PLUGIN_URI},
                             separators=(",", ":"))
        self._call(f"EQSourceOff:{payload}")

    def peq_on(self, source: str) -> None:
        payload = json.dumps({"source_name": source, "pluginURI": PEQ_PLUGIN_URI},
                             separators=(",", ":"))
        self._call(f"EQChangeSourceFX:{payload}")

    def set_band(self, source: str, index: int, band: PeqBand,
                 preamp_adjust: float = 0.0) -> None:
        """Writes one PEQ band. `index` is 0..9."""
        if not 0 <= index < N_BANDS:
            raise ValueError(f"band index out of range: {index}")
        letter = BAND_LETTERS[index]
        adjusted_gain = band.gain + preamp_adjust  # preamp is negative → cuts gain
        payload = {
            "pluginURI":   PEQ_PLUGIN_URI,
            "source_name": source,
            "EQBand": [
                {"param_name": f"{letter}_mode", "value": FILTER_TYPE_CODE[band.type]},
                {"param_name": f"{letter}_freq", "value": round(band.fc, 2)},
                {"param_name": f"{letter}_q",    "value": round(band.q, 3)},
                {"param_name": f"{letter}_gain", "value": round(adjusted_gain, 2)},
            ],
            "EQStat":      "On",
            "channelMode": "Stereo",
        }
        blob = json.dumps(payload, separators=(",", ":"))
        self._call(f"EQSetLV2SourceBand:{blob}")

    def peq_save_name(self, source: str, name: str) -> None:
        payload = json.dumps(
            {"pluginURI": PEQ_PLUGIN_URI, "source_name": source, "Name": name},
            separators=(",", ":"),
        )
        self._call(f"EQSourceSave:{payload}")

    def clear_unused_bands(self, source: str, used: int) -> None:
        """Zero out bands the profile didn't use, so stale settings don't bleed
        through from a previous profile."""
        flat = PeqBand(type="PK", fc=1000.0, gain=0.0, q=1.0)
        for i in range(used, N_BANDS):
            self.set_band(source, i, flat)


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
                    choices=("subtract", "warn", "ignore"), default="subtract",
                    help="How to handle the profile's preamp. 'subtract' lowers "
                         "every band's gain by the preamp amount to avoid "
                         "clipping (default). 'warn' just prints it. 'ignore' "
                         "uses the gains verbatim.")
    ap.add_argument("--http", action="store_true",
                    help="Use http:// instead of https://. Avoids self-signed "
                         "cert hassles; works on most WiiM firmwares.")
    ap.add_argument("--dry-run", action="store_true",
                    help="Print every URL instead of calling the device.")

    args = ap.parse_args(argv)

    client = WiimClient(args.ip, use_http=args.http, dry_run=args.dry_run)

    # --off: just disable PEQ on the source and exit.
    if args.off:
        print(f"turning PEQ off on source='{args.source}' …")
        client.peq_off(args.source)
        print("done.")
        return 0

    # Load the profile (from disk or AutoEQ).
    if args.file:
        with open(args.file, encoding="utf-8") as fh:
            text = fh.read()
        origin = args.file
    else:
        text, origin = fetch_profile_by_name(
            args.headphone, args.measurement, args.target)

    profile = parse_profile(text)
    print(f"\nparsed profile from: {origin}")
    print(f"  preamp: {profile.preamp:+.2f} dB")
    print(f"  bands:  {len(profile.bands)}")
    for i, b in enumerate(profile.bands):
        print(f"    [{i}] {b.type}  Fc={b.fc:>7.1f} Hz  "
              f"Gain={b.gain:+6.2f} dB  Q={b.q:.3f}")

    # Preamp handling.
    adjust = 0.0
    if args.preamp_mode == "subtract":
        adjust = profile.preamp  # preamp is ≤ 0, so this lowers band gains
        if profile.preamp < 0:
            print(f"  → subtracting preamp ({profile.preamp:+.2f} dB) from "
                  f"every band's gain to avoid clipping.")
    elif args.preamp_mode == "warn" and profile.preamp < 0:
        print(f"  ! profile asks for {profile.preamp:+.2f} dB preamp. "
              f"WiiM has no preamp field — lower the device's volume limit "
              f"by that amount manually, or rerun with --preamp-mode=subtract.")

    preset_name = args.headphone if args.headphone else args.file

    # Push to device: write bands → save name → EQChangeSourceFX to activate.
    print(f"\nwriting to WiiM at {args.ip} (source='{args.source}') …")
    for i, band in enumerate(profile.bands):
        client.set_band(args.source, i, band, preamp_adjust=adjust)
    # Flatten bands the profile didn't use.
    client.clear_unused_bands(args.source, used=len(profile.bands))
    client.peq_save_name(args.source, preset_name)
    client.peq_on(args.source)

    print("\ndone. open the WiiM Home app and navigate Device → EQ → "
          "Parametric EQ on the same source to verify.")
    print("(the app UI won't refresh until you leave and re-enter the EQ "
          "screen — this is a known quirk of the unofficial API.)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
