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

In the **TPV** column below: **✓** is a standard TPV field carrying its standard
meaning, **·** is ours, and **⚠** is a TPV field *name* used with a different meaning
here.

| Field | Type | TPV | Source | Notes |
|---|---|---|---|---|
| `id` | slug pair | · | both | **the addressable identity**, `<owner>/<device>`, e.g. `jane/keys`. Fills TPV's `device` role — see below |
| `owner` | slug | · | both | who carries it, e.g. `jane` |
| `device` | slug | ⚠ | both | which of theirs, e.g. `keys`. Shares a name with TPV's `device`, which is the *originating GPS receiver* (`/dev/ttyUSB0`) — see below |
| `upstream_id` | string | · | both | Apple's own opaque identifier, for correlation and registry lookups. **Not addressable.** Not called `apple_id`, which in this project means the iCloud *account* (`FINDMY_REST_APPLE_ID`) |
| `display_name` | string | · | both | Apple's display name; freeform, may contain emoji. For humans, not for addressing |
| `kind` | `accessory` \| `idevice` | · | both | what sort of thing it is, not which backend saw it. Derived from `device_type`; provisionally `accessory` until a report has been decrypted |
| `source` | `findmy` \| `fmip` | · | both | which backend produced this record |
| `lat` | float | ✓ | both | **optional** — see below |
| `lon` | float | ✓ | both | **optional** |
| `alt` | float | ✓ | fmip | altitude, metres. TPV deprecates `alt` for `altHAE`/`altMSL`; Apple does not say which datum it means, so neither do we |
| `time` | ISO 8601 UTC | ✓ | both | when the fix was *recorded*, not when it was fetched |
| `eph` | float | ✓ | both | horizontal error, metres |
| `epv` | float | ✓ | fmip | vertical error, metres |
| `battery_level` | `full` \| `medium` \| `low` \| `critical` | · | both | accessories: bits 6–7 of the status byte. iCloud devices: bucketed from `battery_pct` |
| `battery_pct` | int 0–100 | · | both | iCloud devices: measured. Accessories: **derived** from the level, see below |
| `battery_estimated` | bool | · | both | true when `battery_pct` was derived rather than measured |
| `status_raw` | int | · | findmy | raw accessory status byte, unmodified. Carries a device type as well as the battery level — see below |
| `device_type` | `apple_device` \| `airtag` \| `third_party` \| `airpods` | · | findmy | bits 5–4 of the status byte; absent until a report has been decrypted |
| `confidence` | int | · | findmy | Apple's confidence in the fix. FindMy.py documents 1–3, but **0 occurs in practice** — do not validate against that range |
| `device_status` | `online` \| `offline` \| `pending` \| `unregistered` | · | fmip | mapped from FMIP's numeric status |
| `has_location` | bool | · | both | computed; always present |
| `location_age_s` | float | · | both | computed; seconds since `time`, absent when there is no fix |

Null fields are omitted rather than sent as `null`, so absence is the signal.

### Why the rest have no TPV name

Not for want of looking: **GPSD's schema has no representation at all** — in TPV or in
any other object — for battery level, for a device being online or unreachable, or for
the age of a piece of data (`dgpsAge` is specific to DGPS corrections). GPSD describes
a GPS receiver attached to the machine, which is always powered and always present, so
the questions this API exists to answer are ones it never had to ask. Those fields are
necessarily ours.

Three near-misses are worth naming, because they are the ones a consumer might
otherwise expect:

- **`has_location` is TPV's `mode` in boolean form.** `mode` is `0=unknown, 1=no fix,
  2=2D, 3=3D`, and `has_location` is exactly `mode >= 2`. It is not emitted as `mode`
  because we cannot honestly fill in the rest: an accessory report has no altitude, and
  an iCloud device's altitude does not come with any statement of whether it was a real
  3D fix, so choosing between `2` and `3` would be a guess wearing a standard field
  name.
- **`status` is deliberately avoided.** TPV's is a GNSS fix type (`1=Normal`, `2=DGPS`,
  `3=RTK Fixed`…), which is nothing like Apple's status byte or an iCloud device's
  reachability. Hence `status_raw` and `device_status`: a consumer that already reads
  `status` from a real receiver will never be silently handed one of ours under a name
  it thinks it understands. `confidence` is likewise not any of GPSD's error estimates —
  it is Apple's own opaque number, and `eph` is where the metres live.
- **`epx`/`epy` are absent** because Apple gives a single horizontal accuracy rather
  than per-axis error, and `eph` is precisely the field for that.

`device` shares a name with TPV's, and that is deliberate. Note what it is *not*: the
`status` case above is a genuine hazard, because TPV's `status` is a small integer from
a fixed enumeration and Apple's is a bit-packed byte — same name, incompatible values,
so a transform written against one would silently misread the other. Nothing like that
applies here. TPV's `device` is a string naming the device; ours is a string naming the
device. Ours is merely *scoped* to an owner, with the fully-qualified form in `id` —
which is the field actually filling TPV's role, since a whole tracker is `jane/keys`
where TPV would have `/dev/ttyUSB0`.

Renaming it would also desync the field from the `<owner>.<device>.json` convention
that the key filenames, the SSM parameter names and the ExternalSecret rewrite are all
built on — real churn, to settle a name that misleads nobody.

### Identity: address by `id`

**`id` is `<owner>/<device>`** — both halves slugs matching `^[a-z0-9][a-z0-9-]*$`, so
it is safe in an MQTT topic, a filename, a URL path, or a metric label.
`findmy/jane/keys/location` drops straight out of it. The halves are on the wire too,
so building that topic needs no string surgery.

Neither identifier Apple gives us could serve, which is why we assign our own:

- **`upstream_id`** contains `#` — an MQTT *multi-level wildcard* — plus `/`, `§` and
  `¶`. A real one, with its identifying middle redacted:
  `a:/00000000-0000-0000-0000-...~#¶§§...`. Publishing to a topic built from that would
  not merely look ugly, it would corrupt routing.
- **`display_name`** is freeform: `Jane’s Apple\xa0Watch` carries a typographic
  apostrophe and a non-breaking space; accessories also have an emoji field, and names
  like `Kayak 🐬` are normal. It also changes whenever someone renames the tag in Find
  My.

Both are still published — for correlating against Find My, for registry lookups, and
for showing a human something they recognise — but neither is an address.

> **Changed in 0.2.0.** `id` previously held Apple's opaque string and had to be
> documented as *not* addressable, which is a trap: the field named like an identifier
> was the one you must not use. Apple's string moved to `upstream_id`, the old `name`
> became `display_name`, and `id` now holds the slug pair. If you are upgrading, `id` →
> `upstream_id` and `name` → `display_name`, and anything keyed on the old `id` should
> move to the new one, which is stabler.

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

### What else is in the status byte

`status_raw` is passed through unmodified so you can derive more from it than we do.
Apple documents none of this; the layout below comes from FindMy.py's BLE scanner,
which decodes the same byte as advertised by the accessory:

| Bits | Meaning | Values |
|---|---|---|
| 7–6 | battery level | `0b00` full, `0b01` medium, `0b10` low, `0b11` critical (FindMy.py calls the last "Very Low"; normalised here so the vocabulary matches across backends) |
| 5–4 | device type | `0b00` Apple device, `0b01` AirTag, `0b10` licensed third-party Find My device, `0b11` AirPods |
| 3–0 | unknown | observed as `0b0000` on every report seen so far |

Bits 7–6 are surfaced as `battery_level` and bits 5–4 as `device_type`. The
device-type bits are decoded by FindMy.py in the *scanning* path rather than the
*reports* path, but the same encoding does appear to hold for report bytes — `0xD0`
(`0b11010000`) from an AirTag decodes as critical battery on an AirTag, and `0x00`
from an Apple Watch as full battery on an Apple device, both of which are correct.
Treat that as a well-supported inference rather than a documented guarantee: it rests
on an undocumented format, and a third-party tracker is the case most likely to
deviate. `status_raw` stays on the wire unmodified so you can second-guess us.

`device_type` is also what sets `kind`: `apple_device` means `idevice`, everything
else means `accessory`. That is deliberately a statement about the **thing**, not
about which backend saw it — an iPhone found through exported accessory keys is
still an iPhone, and `source` is the field that records the observer. Battery
alerting depends on the distinction, since "replace this tracker's cell" and "this
phone wants a charger" are different messages.

Both fields are absent until a report has actually been decrypted, because the byte
only exists in a report. `kind` therefore reads `accessory` for a device that has
never been heard from — provisional rather than measured. Once learned it is
remembered across quiet polls, so it does not flap.

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

### One device, two backends

An iPhone, a Watch or a Mac can be visible through both backends at once: the
accessory backend decrypts its offline-finding beacons, and the FMIP backend asks
iCloud where it is. Those are two observations of one thing, so **the response
contains one object per `id`, carrying the fresher fix** — never two.

Which backend wins flips with the state of the device, and both answers are right:

- **Online** — it reports to Apple directly and emits no beacons, so the accessory
  backend has only an ageing fix while FMIP has a current one. In practice FMIP's is
  also far better: ~5 m against ~98 m for the same phone.
- **Powered off or in airplane mode** — it beacons, and FMIP has nothing newer than
  the moment it went offline. The accessory backend wins, which is exactly when its
  position matters most.

A record with no fix never displaces one that has a location, so an iCloud device
sharing battery but not position cannot hide a beacon fix. `source` on the returned
object tells you which backend it came from, and it is expected to change over time
for the same device.

Pass `source=findmy` or `source=fmip` to see a single backend's unmerged view.

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
