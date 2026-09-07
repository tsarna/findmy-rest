# Changelog

All notable changes to this project are documented here.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

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

[0.1.0]: https://github.com/tsarna/findmy-rest/releases/tag/v0.1.0
