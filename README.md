# wiim-autoeq

Apply [AutoEQ](https://github.com/jaakkopasanen/AutoEq) headphone equalization
profiles to your [WiiM](https://www.wiimhome.com/) streamer over the local
network — with a command-line tool or a local web UI.

![web UI screenshot](screenshots/web-ui.png)

## What this does

AutoEQ publishes parametric-EQ profiles for ~6,000 headphones. WiiM streamers
support a 10-band parametric EQ per input source, but the Home app doesn't
have a way to import those profiles. This project bridges the two:

- picks a headphone from the AutoEQ catalog
- downloads the matching `ParametricEQ.txt`
- pushes all 10 bands (plus the preamp) to your WiiM over its local HTTP API
- clears any stale bands left over from a previous profile

There are two interfaces to the same underlying logic:

- **CLI** (`src/wiim_autoeq.py`) — scriptable, good for automation
- **Web UI** (`src/wiim_autoeq_web.py`) — runs a local Flask server with mDNS
  discovery so you don't have to hunt for your WiiM's IP

## Install

Requires Python 3.9 or later.

```
git clone https://github.com/weidercs/wiim-autoeq.git
cd wiim-autoeq
pip install -r requirements.txt
```

## Usage — web UI (recommended)

```
python3 src/wiim_autoeq_web.py
```

Then open <http://127.0.0.1:5173/> in your browser.

The page auto-discovers WiiM devices on your LAN via mDNS/Bonjour. Pick
yours from the dropdown, click **Test connection**, search for your
headphone in the AutoEQ list, click **Apply PEQ**. Done.

If mDNS discovery can't see your device — common on segmented networks
like separate VLANs — click **Enter IP manually** and type the IP from
the WiiM Home app (Device Settings → Network Status).

## Usage — CLI

Apply a profile by headphone name (fuzzy-matched against the AutoEQ index):

```
python3 src/wiim_autoeq.py --ip 192.168.1.42 --headphone "Sennheiser HD 600"
```

Apply a local ParametricEQ.txt file:

```
python3 src/wiim_autoeq.py --ip 192.168.1.42 --file my_profile.txt
```

Turn the PEQ off:

```
python3 src/wiim_autoeq.py --ip 192.168.1.42 --off
```

Other useful flags:

- `--source wifi|line-in|bluetooth|optical|coaxial|hdmi|phono|usb` — which
  input source to write the EQ to (default: `wifi`)
- `--preamp-mode subtract|warn|ignore` — how to handle the profile's preamp
  value (default: `subtract`, since WiiM has no preamp slider)
- `--http` — use plain HTTP instead of HTTPS (try this if you get SSL errors)
- `--dry-run` — show what would be sent without actually calling the device

## How it works

WiiM devices expose a self-signed HTTPS API at
`https://<device-ip>/httpapi.asp?command=...`. A few of the commands are
[officially documented](https://www.wiimhome.com/pdf/HTTP%20API%20for%20WiiM%20Products.pdf)
(like `getStatusEx`, which this tool uses to verify the device responds),
but the PEQ endpoints are not — they were reverse-engineered by the
[devicePEQ](https://github.com/jeromeof/devicePEQ) project from the official
WiiM Home app.

Applying a profile looks like this on the wire:

1. `EQSourceOff` — disable the EQ on that source
2. Ten `EQSetLV2SourceBand` calls writing bands `a_` through `j_`
3. `EQChangeSourceFX` — re-enable the EQ with the new bands

The browser can't talk to the WiiM directly because of the self-signed TLS
cert and CORS, which is why the web UI runs as a small local Flask server:
the browser talks to localhost, and Python talks to the WiiM.

## Caveats

**The PEQ endpoints are unofficial.** WiiM support has stated these are
"closed" APIs. They work as of firmware `Linkplay.5.x` (early 2026) and
are what the official WiiM Home app uses, but a future firmware update
could break them.

**The WiiM Home app caches the EQ view.** After you apply a profile, the
app won't show the new values until you leave the EQ screen and come back.
This is a UI quirk in the Home app, not a sign of failure — the EQ is
applied and audible immediately.

**mDNS discovery requires same-LAN broadcast.** If your WiiM and your
computer are on different VLANs or subnets without an mDNS repeater,
auto-discovery won't find the device. Use the manual-IP fallback.

## Credits

- [jaakkopasanen/AutoEq](https://github.com/jaakkopasanen/AutoEq) — the
  headphone measurements and parametric EQ profiles this tool consumes
- [jeromeof/devicePEQ](https://github.com/jeromeof/devicePEQ) — open-source
  reverse engineering of the WiiM PEQ endpoints
- Measurement sources within AutoEQ, in priority order:
  oratory1990, Innerfidelity, Rtings, Headphone.com

## License

MIT — see [LICENSE](LICENSE).
