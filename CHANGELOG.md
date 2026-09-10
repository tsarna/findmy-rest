# Changelog

All notable changes to this project are documented here.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [0.5.0] - 2026-09-09

### Added

- **`FINDMY_REST_FMIP_MIN_FETCH_INTERVAL`** (default `900`): the iCloud backend now has
  its own fetch floor instead of sharing `FINDMY_REST_MIN_FETCH_INTERVAL` with the
  accessory backend. The two cost Apple different things — the accessory path reads
  reports Apple already holds, while an FMIP fetch asks Apple to *locate* real devices,
  which reaches out to them. Sharing one interval meant polling both at whatever suited
  the cheap one.

### Fixed

- **The iCloud backend no longer lets pyicloud discard a working session.** When the
  `findme` service token expires, pyicloud's own recovery calls
  `authenticate(force_refresh=True)`, throwing away a valid trusted session for a full
  SRP re-login — which Apple refuses often enough to matter. Worse, every API error
  during SRP is relabelled `Invalid email/password combination` regardless of cause, so
  the failure reads as a credentials problem it is not. Observed in practice: a fetch
  died that way with the password provably correct.

  The backend now calls plain `authenticate()` before touching the device manager, which
  reuses the trusted session and re-runs `accountLogin` — all an expired *service* token
  actually needs. Both the `.devices` property and `refresh()` route through the bad
  recovery, so this has to happen before either.

## [0.4.0] - 2026-09-09

### Changed — behaviour

- **`/devices` returns one object per `id`.** A phone, watch or Mac can be visible
  through both backends at once — the accessory backend decrypts its offline-finding
  beacons while FMIP asks iCloud — and those two records previously both appeared,
  sharing an `id`. The response now carries whichever holds the **fresher fix**.

  This is not merely deduplication. Which backend is right flips with the state of
  the device: while it is online it emits no beacons, so FMIP wins and brings a far
  better fix (~5 m against ~98 m for the same phone); while it is off or in airplane
  mode it beacons and FMIP is frozen at the moment connectivity stopped, so the
  accessory backend wins — exactly when its position matters most. A record with no
  fix never displaces one that has a location.

  `source` on the returned object says which backend produced it, and is expected to
  change over time for the same device. `?source=findmy` / `?source=fmip` still give
  a single backend's unmerged view.

- **`kind` now describes the thing, not the backend that saw it.** An iPhone or Watch
  reached through exported accessory keys previously reported `kind: accessory`,
  because the accessory backend hardcoded it. It is now derived from the device-type
  bits of the status byte, so such devices report `idevice`.

  Consumers filtering on `kind` will see the change — notably battery alerting, where
  the point of the field is to separate "replace this tracker's cell" from "this phone
  wants a charger". If `kind` is a metric label, expect existing series for those
  devices to stop and new ones to start.

  The value is provisional until a report has actually been decrypted, since the byte
  only exists in a report; a device never heard from still reads `accessory`. Once
  learned it is remembered across polls that decrypt nothing, so it does not flap.

### Added

- `device_type`: `apple_device` | `airtag` | `third_party` | `airpods`, decoded from
  bits 5–4 of the accessory status byte. `status_raw` remains on the wire unmodified.

## [0.3.0] - 2026-09-08

### Changed

- **The published image now includes the optional FMIP extra.** The workflow passes
  `EXTRAS=[fmip]`, so pyicloud ships in the image instead of only being installed in
  CI. Previously the backend could be enabled by configuration but would fail on its
  first poll, because the package it imports was not there.

  This does **not** turn the backend on. It remains off unless
  `FINDMY_REST_ENABLE_FMIP` says otherwise, and enabling it still requires a separately
  authenticated iCloud session. One image rather than a second tag stream: the extra is
  a few MB, and a variant would double the release surface for very little.

### Added

- The image smoke test asserts pyicloud is importable inside the published image.
  The backend imports it lazily, so a missing extra is invisible at startup and would
  otherwise surface only as an exception on the first poll after someone enabled it.

## [0.2.0] - 2026-09-07

### Changed — breaking

The device object's identity fields were rearranged. `id` previously held Apple's
opaque identifier and had to be documented as **not** addressable, which is a trap: the
field named like an identifier was the one you must not use.

- **`id` is now the addressable slug pair**, `<owner>/<device>` (e.g. `jane/keys`). It
  is derived, so it cannot disagree with the halves, which remain published separately
  for building topic segments.
- **`id` → `upstream_id`** for Apple's opaque identifier. Not `apple_id`, which in this
  project already means the iCloud *account* (`FINDMY_REST_APPLE_ID`).
- **`name` → `display_name`** for Apple's freeform display name, making it obvious the
  field is for humans rather than addressing.

To upgrade a consumer: `id` → `upstream_id`, `name` → `display_name`, and anything
keyed on the old `id` should move to the new one, which is stabler across renames.

### Added

- `/health` reports the running `version`, so a pinned deployment can be checked
  without exec'ing into the container.
- [docs/HOWTO.md](docs/HOWTO.md): deployment end to end, from exported keys through
  Docker Compose or a bare CLI to the one-time login, plus a Kubernetes section.
- `docs/api.md` now marks which fields are standard GPSD TPV and which are ours, and
  documents the device-type bits in the accessory status byte.

## [0.1.0] - 2026-09-07

First release. Serves Find My accessory location and battery over HTTP, from
exported accessory keys, with no Apple hardware in the loop.

### Added

- `GET /devices` — location and battery for every known accessory, served from
  cache so the response time does not depend on Apple's. Filters: `source`,
  `located`, `max_age`, and `wait` to block for a live refresh.
- `GET /health` — per-backend `auth_required`, `last_fetch`, `last_error`,
  `refreshing` and `deferred` counts, plus `two_factor_pending` and the running
  version.
- `POST /login`, `POST /login/2fa/request`, `POST /login/2fa` — unattended
  re-authentication. A requested code is accepted only inside a bounded window,
  and only once, so an SMS-driven automation cannot be replayed.
- Accessory backend: batched report fetches, local (Unicorn) anisette, and an
  Apple session persisted across restarts so a password and 2FA are needed once
  rather than every poll.
- Optional FMIP backend (`fmip` extra) for iCloud devices — phones and laptops
  rather than tags. Off by default.
- Key alignment cache. Rolling-key derivation ratchets forward from the last
  known index, so an accessory that has been silent costs minutes of CPU on a
  cold start; persisting the index turns that into a few hundred derivations.
- Cost-proportional polling. An accessory's index range widens by ~96 a day
  while it is not reporting and can never be narrowed except by a report, so
  accessories exceeding `POLL_BUDGET` share it and are polled in inverse
  proportion to their cost. They are deferred, never dropped: their last known
  position is still served, and one that starts reporting again returns to
  every-cycle polling immediately.
- Stable `owner`/`device` identity for each accessory, from the key filename,
  an explicit registry, or a default owner — Apple's names and ids are neither
  stable nor safe as topic segments.
- Both battery representations on every device: a coarse
  `full`/`medium`/`low`/`critical` level and a `battery_pct`, each synthesised
  from the other when only one is available, so consumers can graph a number
  without special-casing accessories.
- `scripts/sync_ssm.py` — push a directory of accessory keys to AWS SSM
  Parameter Store, with optional 1Password backup.
- `scripts/verify_reports.py` — fetch and decrypt live reports from exported
  keys, for proving a set of keys works before deploying it.
- Multi-arch (amd64/arm64) container image, built on native runners and
  published to `ghcr.io/tsarna/findmy-rest`.

[0.2.0]: https://github.com/tsarna/findmy-rest/releases/tag/v0.2.0
[0.1.0]: https://github.com/tsarna/findmy-rest/releases/tag/v0.1.0
