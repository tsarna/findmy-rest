# API reference

Base URL is wherever the service listens (`FINDMY_REST_HOST`/`_PORT`, default
`0.0.0.0:8080`). There is no authentication on the API itself — it is designed to sit on
a private network or behind an ingress that handles that, and it holds a live Apple
session, so **do not expose it publicly**.

Interactive docs are generated from the running service at `/docs`, and the raw schema at
`/openapi.json`. This file explains the parts a schema cannot: which backend fills which
field, and what the failure modes mean.

## The device object

One shape for both backends. Field names follow [GPSD
TPV](https://gpsd.gitlab.io/gpsd/gpsd_json.html) wherever an equivalent exists, so output
flows into consumers that already speak that vocabulary without translation. Fields with
no TPV equivalent keep their own names.

| Field | Type | Source | Notes |
|---|---|---|---|
| `id` | string | both | Apple's own identifier. Opaque — **not addressable**, see below |
| `name` | string | both | Apple's display name; freeform, may contain emoji |
| `kind` | `accessory` \| `idevice` | both | what sort of thing it is |
| `source` | `findmy` \| `fmip` | both | which backend produced this record |
| `owner` | slug | both | who carries it, e.g. `jane` |
| `device` | slug | both | which of theirs, e.g. `keys` |
| `lat` | float | both | **optional** — see below |
| `lon` | float | both | **optional** |
| `alt` | float | fmip | altitude, metres |
| `time` | ISO 8601 UTC | both | when the fix was *recorded*, not when it was fetched |
| `eph` | float | both | horizontal error, metres |
| `epv` | float | fmip | vertical error, metres |
| `battery_level` | `full` \| `medium` \| `low` \| `critical` | both | accessories: bits 6–7 of the status byte. iCloud devices: bucketed from `battery_pct` |
| `battery_pct` | int 0–100 | both | iCloud devices: measured. Accessories: **derived** from the level, see below |
| `battery_estimated` | bool | both | true when `battery_pct` was derived rather than measured |
| `status_raw` | int | findmy | raw accessory status byte, unmodified |
| `confidence` | int | findmy | Apple's confidence in the fix. FindMy.py documents 1–3, but **0 occurs in practice** — do not validate against that range |
| `device_status` | `online` \| `offline` \| `pending` \| `unregistered` | fmip | mapped from FMIP's numeric status |
| `has_location` | bool | both | computed; always present |
| `location_age_s` | float | both | computed; seconds since `time`, absent when there is no fix |

Null fields are omitted rather than sent as `null`, so absence is the signal.

### Identity: use `owner`/`device`, never `id` or `name`

Neither field Apple gives us can be used as an MQTT topic segment, a filename or a
metric label:

- **`id`** contains `#` — an MQTT *multi-level wildcard* — plus `/`, `§` and `¶`. A real
  one, with its identifying middle redacted: `a:/00000000-0000-0000-0000-...~#¶§§...`.
  Publishing to a topic built from that would not merely look ugly, it would corrupt
  routing.
- **`name`** is freeform: `Jane’s Apple\xa0Watch` carries a typographic apostrophe and a
  non-breaking space; accessories also have an emoji field, and names like `Kayak 🐬`
  are normal.

So every device carries an explicit `owner` and `device`, both slugs matching
`^[a-z0-9][a-z0-9-]*$`, and `slug` is `<owner>/<device>` — safe in an MQTT topic, a
filename, a URL path, or a metric label. `findmy/jane/keys/location` drops straight out
of it.

Slugs resolve in this order:

1. **The registry**, a JSON file at `FINDMY_REST_REGISTRY` (a ConfigMap in Kubernetes),
   mapping an Apple name *or* id to `"<owner>/<device>"`. This is the override, and the
   only mechanism for iCloud devices, which arrive with no file of their own:
   ```json
   {"devices": {"Jane’s Keys": "jane/keys", "2006~#0000...": "jane/wallet"}}
   ```
2. **The key filename**, `<owner>.<device>.json`. The operator already chooses this when
   putting the key into SSM, so the parameter name and the MQTT topic agree by
   construction, with nothing extra to maintain. `jane.keys.json` → `jane/keys`.
3. **Slugified name under `FINDMY_REST_DEFAULT_OWNER`**. A newly-paired accessory keeps
   working rather than disappearing; it shows up as e.g. `family/johns-luggage`, which
   is the visible cue to give it a proper name.

Apostrophes are deleted rather than split on, and every variant identically, so
`John's` with a straight quote and `John’s` with the curly one Apple actually uses both
yield `johns` — otherwise the same name typed two ways
would produce two topics. A malformed registry entry is logged and falls back rather
than taking the service down.

Slugs are **stable across renames in Find My** when they come from the registry or a
filename, which is the point: renaming an AirTag in the app should not silently
re-route your MQTT topics or orphan your dashboards.

### `lat`/`lon`/`time` are optional, and that is normal

An iCloud device whose owner has not enabled location sharing returns **battery only**.
That is a steady state, not a transient error, and it will never resolve on its own. Do
not treat a missing fix as a failure; either filter for what you need with
`?located=true` or branch on `has_location`.

Accessories behave differently: a Find My report *is* a location fix carrying a status
byte, so an accessory either has both location and battery or has neither. Filtering by
location therefore never costs you accessory battery data.

### Battery: both fields, always, but read the flag

Apple gives accessories four levels and never a percentage; iCloud devices give a
percentage and no level. Reporting them as-is would mean a dashboard needs two series
and two alert rules for the same question, so each backend's missing half is filled in:

| level | percentage | a percentage in this range reads as |
|---|---|---|
| `full` | 100 | 100–85 |
| `medium` | 70 | 85–55 |
| `low` | 40 | 55–25 |
| `critical` | 10 | 25–0 |

A percentage takes the level whose stand-in value it is *nearest*, so the cut points are
the midpoints — 85, 55, 25 — derived from the mapping itself rather than written out, so
the two directions cannot drift apart. Boundaries round up: 85 is `full`. Cutting at the
stand-ins themselves would mean a device is only `full` at exactly 100%, and effectively
never full once it leaves the charger.

Every level survives a round trip through a percentage unchanged, so an accessory's
reported level and its plotted value cannot disagree.

**`battery_estimated` is true whenever the percentage was derived.** For an accessory
there is no measurement behind it: 10 is chosen because that is where iOS and macOS turn
the indicator red, and the values are the top of each band, so they read optimistically.
Plot them as a shape; **alert on `battery_level`**, which is what the hardware actually
reported.

`status_raw` is passed through unmodified for accessories if you want to derive something
else from it. The level mapping follows FindMy.py's own scanner: bits 6–7 of the status
byte, `0b00`=full, `0b01`=medium, `0b10`=low, `0b11`=critical (FindMy.py calls the last
"Very Low"; normalised here so the vocabulary matches across backends).

## `GET /devices`

Returns a JSON array of device objects. Unfiltered by default — upstream truth first,
consumers narrow.

| Parameter | Default | Effect |
|---|---|---|
| `source` | all | `findmy` or `fmip`; lets each backend be polled at its own cadence |
| `located` | `false` | drop devices with no location |
| `max_age` | none | drop fixes older than this: `900`, `30s`, `15m`, `2h`, `1d` |
| `force` | `false` | bypass the minimum fetch interval |

```bash
curl 'http://localhost:8080/devices?located=true&max_age=30m'
```

`located` and `max_age` do different jobs. `located=true` removes devices that never had
a fix; `max_age` removes devices carrying an *ancient* fix — typically retired hardware
still lingering in the account. Account cruft that neither catches should go in
`FINDMY_REST_EXCLUDE`, which matches against name **and** model (an AirPods case reports
as `Case`, `left` and `right`, so only the model identifies it).

A malformed `max_age` returns **400**.

### Polling and caching

`/devices` returns what is known **immediately** and refreshes in the background when the
cache is older than `FINDMY_REST_MIN_FETCH_INTERVAL` (default 60s). The first call after
a cold start can therefore return `[]` or stale data while the first refresh runs; add
`?wait=true` if you want freshness over promptness.

The refresh is off the request path because it can be slow in a way that is not fixable.
Finding a report means deriving every rolling key the accessory could currently be using,
and that band widens by ~96 indices per day for anything that has not reported — the key
index advances with the accessory's *powered* time, so a tracker switched off in October
could be anywhere between where it was last seen and where it would be had it run ever
since. Measured on real data: an aligned tracker is ~670 indices and under a second; one
silent since January was 58,164 indices and 66s; one never aligned, 131,827 and ~150s.
That cost repeats on **every** fetch — a second pass measured 37.4s against the first's
37.9s — because FindMy.py's key cache makes each derivation O(1) but you still perform
all of them.

The practical consequence: **deploy accessories that report.** A silent one costs a
widening walk forever and cannot be located anyway. `/health` shows `refreshing` so you
can tell a slow refresh from a stuck one.

Polling faster than a few minutes buys nothing: report latency is minutes, and an
AirTag's key rotates every 15 minutes. 5 minutes is a sensible accessory cadence, 10+ for
FMIP, where each fetch pings real devices.

**Stale data is served in preference to errors.** If a fetch fails — expired session,
Apple hiccup — the last known devices are returned with their original `time`, and
`location_age_s` grows so the staleness is visible. A consumer polling every five minutes
would rather have an old fix it can reason about than a gap. Check `/health` to
distinguish "nothing moved" from "we haven't been able to ask".

## `GET /health`

```json
{
  "ok": true,
  "findmy": {"enabled": true, "auth_required": false,
             "last_fetch": "2026-09-06T21:14:33Z", "last_error": null, "device_count": 8},
  "fmip":   {"enabled": false, "auth_required": false,
             "last_fetch": null, "last_error": null, "device_count": 0}
}
```

`ok` is **true whenever the service is serving**, including when a backend needs
re-authentication. This is deliberate: wiring a liveness probe to `auth_required` would
restart the pod into a 2FA loop, burning attempts and fixing nothing. A human (or an
automation) has to supply credentials, and the service stays up serving last-known data
until then.

The field to alert on is `auth_required`. The field to graph is `last_fetch` — or better,
`location_age_s` per device, since that captures "her keys haven't been seen in three
hours", which is the signal that actually matters.

`enabled` means the backend has something to do: for `findmy`, that accessory keys
loaded; for `fmip`, that `FINDMY_REST_ENABLE_FMIP` is set.

## Authentication endpoints

Needed on first run, and again whenever Apple invalidates the session. State persists to
`FINDMY_REST_STATE_DIR`, so this should be rare — if it is happening often, the state
directory is probably not persistent.

### `POST /login`

```bash
curl -X POST http://localhost:8080/login -H 'content-type: application/json' -d '{}'
```

Body is optional; `{"password": "..."}` overrides `FINDMY_REST_PASSWORD`. Returns the
login state and, when 2FA is required, the available methods:

```json
{"state": "LoginState.REQUIRE_2FA",
 "methods": [{"index": 0, "type": "AsyncTrustedDeviceSecondFactor", "phone_number": null},
             {"index": 1, "type": "AsyncSmsSecondFactor", "phone_number": "+1 ••• •• •• 37"}]}
```

### `POST /login/2fa/request?method_index=N`

Sends the code by the chosen method. Trusted-device push is index 0 in the usual case;
pick the SMS entry matching the automation number if you have one.

### `POST /login/2fa`

```bash
curl -X POST http://localhost:8080/login/2fa -H 'content-type: application/json' \
  -d '{"code": "123456", "method_index": 0}'
```

Returns `{"state": ..., "auth_required": false}` on success, and the session is written to
disk immediately — a successful 2FA round trip is the expensive thing here and is never
risked on a later failure.

**Do not retry a failed login in a loop.** Apple rate-limits: a burst of authentication
attempts earns a `503` from its GSA endpoint, and rapid retries extend the lockout rather
than clearing it. Wait 20–30 minutes and try once.

### Unattended 2FA, and its guard rails

`FINDMY_REST_2FA_PREFER` decides which method an unattended re-auth uses:

| Value | Meaning |
|---|---|
| `sms:19` | the SMS method whose number ends in those digits — **preferred** |
| `sms` | the first SMS method |
| `trusted-device` | the first non-SMS method |
| `1` | a bare index, as an escape hatch |
| *(empty)* | index 0 — wrong for an account with no trusted device |

Prefer a suffix over an index. Apple's ordering is not contractual, so an index can come
to mean a *different phone number* without anything failing — a code sent somewhere
nobody is watching, during exactly the unattended flow you cannot observe. Apple masks
the number (`+1 ••• •• •• 19`), so match only the digits it actually shows.

A code is accepted **only while a request this service made is still in flight**, and
**only once**. `POST /login/2fa` outside that window returns 400, and
`FINDMY_REST_2FA_WINDOW` (default 300s) bounds it. `GET /health` exposes
`two_factor_pending` so an automation can gate on it.

This matters when an SMS webhook is wired to the endpoint. Message buses redeliver, and
every rejected code burns an attempt against the account — enough of them lock it. The
check lives here rather than in the consumer so a buggy or over-eager rule upstream
cannot spam Apple.

Worth being clear about what this does *not* protect. A code is bound by Apple to the
login attempt that triggered it, so submitting one here cannot authenticate someone
else's session. The real exposure is the message bus: if 2FA texts are published to a
topic in plaintext, then whoever can read that topic and knows the password has both
factors. Restrict the topic, and keep code text out of logs and retained messages.
