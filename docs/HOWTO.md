# HOWTO: from exported keys to a running service

Getting the accessory keys is the hard part, and it happens once — see
**[key-export.md](key-export.md)**. Everything after it is a directory of files, a
container, and a single interactive login.

You need two things to start:

- **Exported accessory keys**, one JSON file per tracker.
- **An Apple ID to fetch with.** Any account works — it does **not** need to own the
  accessories, and does not even need to be in the same family. A dedicated account used
  only for this is the tidiest option, since the service holds a logged-in session for
  whatever you give it.

## 1. Lay out the keys

Rename each exported file to **`<owner>.<device>.json`**. Both halves are slugs you
choose: I suggest lowercase letters, digits and dashes, starting alphanumeric.

```
keys/
├── jane.keys.json
├── jane.purse.json
├── john.luggage.json
└── john.bike.json
```

That filename becomes the device's identity everywhere — the service reports
`owner: jane`, `device: keys`, and consumers address it as `jane/keys`. Apple's own
names and identifiers are not usable for this (they contain `#`, `/` and `§`, and
change when someone renames a tag in Find My), which is why **you** pick the name instead.
See [api.md](api.md#identity-use-ownerdevice-never-id-or-name).

Pick owner ids per person and stay consistent. They are yours; nothing checks them
against Apple.

## 2. Point the service at it

Two directories matter:

| Path | What it holds | Notes |
|---|---|---|
| `FINDMY_REST_KEYS_DIR` | the `*.json` key files | read-only is fine |
| `FINDMY_REST_STATE_DIR` | Apple session, anisette identity, alignment cache | **must persist** |

**State must survive restarts.** It holds a logged-in Apple session and an *anisette
identity* — a virtual device registered with Apple. Losing it means redoing the login
and 2FA below on every restart, and presenting Apple with a stream of new devices,
which it rate-limits. It is a few megabytes. Give it a real volume.

### Docker Compose

```yaml
services:
  findmy-rest:
    image: ghcr.io/tsarna/findmy-rest:0.1.0
    ports:
      - "8080:8080"
    environment:
      FINDMY_REST_APPLE_ID: you@example.com
      # Only read during login; the saved session covers normal operation.
      FINDMY_REST_PASSWORD: ${ICLOUD_PASSWORD}
      # Which 2FA method to use when re-authenticating unattended. Set this
      # once you have seen the method list in step 3.
      FINDMY_REST_2FA_PREFER: sms
    volumes:
      - ./keys:/etc/findmy-rest/accessories:ro
      - state:/var/lib/findmy-rest

volumes:
  state:
```

Those two container paths are the image defaults, so no `KEYS_DIR`/`STATE_DIR` needed.

### Or without Docker

```bash
pip install git+https://github.com/tsarna/findmy-rest   # add [fmip] for iCloud devices

FINDMY_REST_APPLE_ID=you@example.com \
FINDMY_REST_PASSWORD=... \
FINDMY_REST_KEYS_DIR=./keys \
FINDMY_REST_STATE_DIR=./state \
  python -m findmy_rest
```

The full environment-variable table is in the [README](../README.md#running-it).

## 3. Log in, once

The service starts without a session and says so rather than crash-looping — an auth
failure is not fatal, because restarting cannot fix it and would only burn 2FA attempts:

```console
$ curl -s localhost:8080/health | jq '.findmy.auth_required'
true
```

Log in. With `FINDMY_REST_PASSWORD` set, the body can be empty:

```console
$ curl -sXPOST localhost:8080/login | jq
{
  "state": "LoginState.REQUIRE_2FA",
  "selected": 1,
  "preference": "sms",
  "methods": [
    {"index": 0, "type": "AsyncTrustedDeviceSecondFactor", "phone_number": null},
    {"index": 1, "type": "AsyncSmsSecondFactor", "phone_number": "(•••) •••-••19"}
  ]
}
```

This list is what `FINDMY_REST_2FA_PREFER` selects from later. Note the *masked*
digits — `sms:19` matches what Apple displays, not the real number. Plain `sms` picks
the only SMS method, which is simpler when there is only one.

Ask Apple to send a code, then submit it:

```console
$ curl -sXPOST 'localhost:8080/login/2fa/request?method_index=1'
{"requested": true, "method_index": 1}

$ curl -sXPOST localhost:8080/login/2fa \
    -H 'content-type: application/json' -d '{"code": "123456"}'
{"state": "LoginState.LOGGED_IN", "auth_required": false}
```

That is the last interactive step. The session is written to the state volume and
restored on every restart.

> A submitted code is only accepted while a request *the service itself made* is in
> flight, and only once. That is what makes it safe to wire the 2FA endpoint to an
> automated SMS handler later: a replayed or unrelated code cannot burn attempts.

## 4. Read it

```console
$ curl -s localhost:8080/devices | jq -r '.[] | "\(.owner)/\(.device)  \(.battery_level)"'
jane/keys      medium
jane/purse     full
john/luggage   full
john/bike      low
```

Reads are served from cache and refreshed in the background, so responses are fast and
*your* poll interval sets freshness. Useful query parameters:

```bash
curl -s 'localhost:8080/devices?located=true'       # only things with a fix
curl -s 'localhost:8080/devices?max_age=30m'        # only reasonably fresh fixes
curl -s 'localhost:8080/devices?wait=true'          # block for a live refresh
```

From here the output is just JSON on an interval — publish it to MQTT, scrape it into
a dashboard, feed a geofence, emit metrics and notify on battery level so a tracker
never dies unnoticed. Nothing about the service assumes any of those.

The one thing worth alerting on: `GET /health` exposes `findmy.auth_required`, which
goes true if Apple ever invalidates the session. Everything else keeps working from
cache until someone repeats step 3.

## Kubernetes

Same shape, different plumbing — a Deployment, a PVC, and a Secret. There is no chart
here to install; the parts that are easy to get wrong:

- **State on a PersistentVolumeClaim**, `ReadWriteOnce`. With `strategy: Recreate` and
  **one replica** — never two. A second pod cannot attach the volume, and two pods
  would fight over one Apple session and one anisette identity.
- **Keys in a Secret**, mounted read-only at `/etc/findmy-rest/accessories`, with one
  Secret key per file named exactly `<owner>.<device>.json`. The service globs `*.json`,
  which conveniently skips the `..data` symlinks a projected Secret volume adds.
- **The registry** (optional, maps Apple names to slugs) is not secret — a ConfigMap.
- **`enableServiceLinks: false`.** Kubernetes injects legacy Docker-link variables for
  every Service in the namespace, so a Service named `findmy-rest` sets
  `FINDMY_REST_PORT=tcp://10.43.x.x:8080` — colliding exactly with this app's config
  prefix, and the container exits with `invalid literal for int()`. Set an explicit
  `FINDMY_REST_PORT` too.
- **`readOnlyRootFilesystem: true` needs writable `/tmp`** (an `emptyDir` is enough):
  anisette unpacks its emulated Apple libraries there at startup.
- **Probe `/health`, but do not restart on `auth_required`.** It stays `ok: true`
  deliberately — a restart cannot re-authenticate, and a restart loop burns 2FA
  attempts.

### Managing the keys

Anything that produces a Secret works. One option, if you already run [External Secrets
Operator][eso] on AWS: keep each key in AWS SSM Parameter Store and let ESO sweep a path, so
adding a tracker is a push rather than a chart edit. `scripts/sync_ssm.py` in this repo
pushes a key directory to SSM (with optional 1Password backup) and enforces the
`<owner>.<device>.json` convention on the way in:

```bash
scripts/sync_ssm.py keys/                      # plan only (the default)
scripts/sync_ssm.py keys/ --apply              # write
scripts/sync_ssm.py keys/ --external-secret    # emit an ExternalSecret data block
```

SSM and ESO are one possibility among many — sealed secrets, SOPS, Vault, or a
hand-made Secret are all fine. The service only cares that the files land in a
directory with the right names.

[eso]: https://external-secrets.io/
