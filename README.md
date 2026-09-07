# findmy-rest

A REST API over Apple Find My: location and battery for accessories (AirTags and Find My
network trackers) and, optionally, for iCloud devices (phones, laptops, watches).

It wraps [FindMy.py](https://github.com/malmeloo/FindMy.py) and the iCloud FMIP endpoint
behind a small HTTP service, so anything that can make an HTTP request — an automation
platform, a dashboard, a home-automation system — can consume Find My data without
speaking Apple's protocols.

> **Unofficial.** Not affiliated with, authorized, or endorsed by Apple Inc.
> "Find My", "AirTag" and "iCloud" are trademarks of Apple Inc.

## What it gives you

`GET /devices` returns a flat list of device objects using [GPSD
TPV](https://gpsd.gitlab.io/gpsd/gpsd_json.html) field names wherever an equivalent
exists, so the output drops into consumers that already speak that vocabulary:

```json
[
  {
    "id": "2006~#0000000000000000~#EXAMPLE00SER",
    "name": "Jane\u2019s Keys",
    "kind": "accessory",
    "source": "findmy",
    "owner": "jane",
    "device": "keys",
    "lat": 37.235,
    "lon": -115.8111,
    "time": "2026-09-06T21:14:33Z",
    "eph": 12.0,
    "battery_level": "medium",
    "status_raw": 65,
    "confidence": 2,
    "has_location": true,
    "location_age_s": 184.2
  }
]
```

Address devices by **`owner`/`device`**, never by `id` or `name`: Apple's
identifiers contain `#` (an MQTT wildcard), `/` and `§`, and names carry emoji and
typographic punctuation. Slugs come from a registry file or the `<owner>.<device>.json`
key filename, so they survive renames in Find My. See [docs/api.md](docs/api.md).

`lat`, `lon` and `time` are **optional**. An iCloud device whose owner does not share
location returns battery only; that is a normal steady state, not an error. Consumers
that need a fix should ask for one rather than assume it:

| Query | Effect |
|---|---|
| `?located=true` | drop devices with no location |
| `?max_age=30m` | drop fixes older than this (`900`, `30s`, `15m`, `2h`, `1d`) |
| `?source=findmy` \| `fmip` | one backend only, so each can be polled at its own cadence |
| `?force=true` | bypass the minimum fetch interval |

Other endpoints: `GET /health` (per-backend `auth_required`, `last_fetch`, `last_error`),
`POST /login`, `POST /login/2fa/request`, `POST /login/2fa`.

## Getting accessory keys

This is the hard part, and it is a prerequisite: Apple offers no API for accessory keys,
so they must be extracted once from a Mac keychain or from iCloud Keychain escrow. See
**[docs/key-export.md](docs/key-export.md)** — which route applies depends on your macOS
version, and on macOS 26 the local-keychain route no longer works at all.

Keys are one-time per accessory and stay valid indefinitely. Only re-pairing an accessory
invalidates them.

## Running it

See **[docs/HOWTO.md](docs/HOWTO.md)** for a walkthrough from exported keys to a
running service, with Docker Compose and Kubernetes examples. The short version:

```bash
pip install git+https://github.com/tsarna/findmy-rest   # add [fmip] for iCloud devices
FINDMY_REST_APPLE_ID=you@example.com \
FINDMY_REST_PASSWORD=... \
FINDMY_REST_KEYS_DIR=/path/to/exported/keys \
FINDMY_REST_STATE_DIR=/var/lib/findmy-rest \
  python -m findmy_rest
```

| Variable | Default | Meaning |
|---|---|---|
| `FINDMY_REST_APPLE_ID` | — | account used to fetch reports (any account works; it need not own the accessories) |
| `FINDMY_REST_PASSWORD` | — | only needed for the initial login |
| `FINDMY_REST_KEYS_DIR` | `accessories` | exported accessory JSON files |
| `FINDMY_REST_STATE_DIR` | `.state` | session, anisette identity and libs — **must persist** |
| `FINDMY_REST_MIN_FETCH_INTERVAL` | `60` | seconds; floor on how often Apple is contacted |
| `FINDMY_REST_EXCLUDE` | — | comma-separated substrings (name and model); matching devices are dropped |
| `FINDMY_REST_REGISTRY` | `/etc/findmy-rest/registry.json` | maps Apple name or id to `<owner>/<device>` |
| `FINDMY_REST_DEFAULT_OWNER` | `unknown` | owner for devices the registry and filenames do not name |
| `FINDMY_REST_2FA_PREFER` | — | which 2FA method unattended re-auth uses: `sms:19`, `sms`, `trusted-device`, or an index |
| `FINDMY_REST_2FA_WINDOW` | `300` | seconds a requested code stays acceptable |
| `FINDMY_REST_ENABLE_FMIP` | `false` | enable the iCloud device backend |
| `FINDMY_REST_HOST` / `_PORT` | `0.0.0.0` / `8080` | listen address |

**`STATE_DIR` must be persistent.** It holds the Apple session and the anisette identity;
losing it means a password and 2FA round trip on every restart, and a stream of new
"devices" logging into your account, which Apple rate-limits.

## Design notes

- **One process, one event loop.** No gunicorn, no worker pool: the Apple session and the
  anisette identity have exactly one owner. Concurrent provisioning against one Apple ID
  is a good way to get flagged.
- **Local anisette**, via Unicorn-emulated Apple libraries. Measured at ~90 MB RSS, 1.1s
  one-time provisioning, ~16ms per call — no anisette server, no third party in the auth.
- **Auth failure is not fatal.** The service keeps serving last-known data, flags
  `auth_required` in `/health`, and waits for `POST /login`. Crash-looping would burn 2FA
  attempts and fix nothing.
- **Fetch on demand, with a floor.** Consumers drive cadence; `min_fetch_interval` stops
  a chatty client from hammering Apple. Apple's report latency is minutes and an AirTag's
  key rotates every 15 minutes, so polling faster buys nothing.

## Licence

MIT. Note that the `anisette` dependency pulls in Unicorn Engine, which is GPLv2; if you
publish container images, keep its licence notices in the image.
