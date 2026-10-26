#!/usr/bin/env python3
"""Create the trusted iCloud session the FMIP backend needs. See docs/HOWTO.md.

The service cannot do this itself: pyicloud's 2FA has no endpoint here, so it is run
once by hand, *where the service runs* (inside the pod), with the service's own
environment. It uses exactly what backends/idevices.py uses -- the Apple ID,
password, `$FINDMY_REST_STATE_DIR/icloud` cookie directory and with_family -- so the
service picks up the session it leaves behind.

The 2FA code is read from a file the operator writes, so the login can wait across
a human round trip without a TTY:

    kubectl exec deploy/findmy-rest -- python3 -u /path/to/fmip_login.py   # waits
    kubectl exec deploy/findmy-rest -- sh -c 'printf %s 123456 > /tmp/fmip-2fa-code'

Success is judged by actually listing devices, not by validate_2fa_code()'s bool,
which reduces a wrong code, an expired one and a failed trust step to the same False.
Any existing session files are renamed `*.stale-<timestamp>`, never deleted.
"""

import logging
import os
import sys
import time
from pathlib import Path

COOKIE_DIR = Path(os.environ.get("FINDMY_REST_STATE_DIR", "/var/lib/findmy-rest")) / "icloud"
CODE_FILE = Path(os.environ.get("FMIP_2FA_CODE_FILE", "/tmp/fmip-2fa-code"))
WAIT_S = 600
STAMP = time.strftime("%Y%m%dT%H%M%S")


class Capture(logging.Handler):
    """Keep pyicloud's own error lines: validate_2fa_code() reduces them to False."""

    def __init__(self):
        super().__init__(logging.WARNING)
        self.lines = []

    def emit(self, record):
        self.lines.append(f"{record.levelname} {record.name}: {record.getMessage()}")


def say(msg):
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


capture = Capture()
logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(name)s: %(message)s")
logging.getLogger().addHandler(capture)

from pyicloud import PyiCloudService

apple_id = os.environ["FINDMY_REST_APPLE_ID"]
password = os.environ["FINDMY_REST_PASSWORD"]

# Set the dead session aside (kept, not deleted) so the login starts clean.
COOKIE_DIR.mkdir(parents=True, exist_ok=True)
for f in COOKIE_DIR.iterdir():
    if f.suffix in (".session", ".cookiejar"):
        f.rename(f.with_name(f"{f.name}.stale-{STAMP}"))
        say(f"set aside {f.name}")
CODE_FILE.unlink(missing_ok=True)

say("signing in")
try:
    api = PyiCloudService(apple_id, password, cookie_directory=str(COOKIE_DIR), with_family=True)
except Exception as exc:  # noqa: BLE001 - report any failure, then exit non-zero
    say(f"SIGN-IN FAILED: {type(exc).__name__}: {exc}")
    for line in capture.lines:
        say(f"  pyicloud: {line}")
    sys.exit(1)

say(
    f"requires_2fa={api.requires_2fa} requires_2sa={api.requires_2sa} trusted={api.is_trusted_session}"
)

if api.requires_2fa:
    sent = api.request_2fa_code()
    say(
        f"2FA requested: sent={sent} via={api.two_factor_delivery_method}"
        f" notice={api.two_factor_delivery_notice!r}"
    )
    say(f"WAITING for code in {CODE_FILE} (up to {WAIT_S}s)")
    deadline = time.time() + WAIT_S
    while not CODE_FILE.exists() and time.time() < deadline:
        time.sleep(2)
    if not CODE_FILE.exists():
        say("TIMED OUT waiting for the code")
        sys.exit(2)
    code = CODE_FILE.read_text().strip()
    CODE_FILE.unlink(missing_ok=True)
    say(f"validating code ({len(code)} digits)")
    ok = api.validate_2fa_code(code)
    say(f"validate_2fa_code -> {ok}; trusted={api.is_trusted_session}")
    if not api.is_trusted_session:
        say(f"trust_session -> {api.trust_session()}; trusted={api.is_trusted_session}")

for line in capture.lines:
    say(f"  pyicloud: {line}")

# Capability, not return values: can this session see devices?
try:
    api.authenticate()
    manager = api.devices
    manager.refresh()
    devices = list(manager)
    with_loc = sum(1 for d in devices if (d.data.get("location") or {}).get("latitude") is not None)
    say(f"DEVICES OK: {len(devices)} devices, {with_loc} with a location")
except Exception as exc:  # noqa: BLE001 - report any failure, then exit non-zero
    say(f"DEVICE FETCH FAILED: {type(exc).__name__}: {exc}")
    sys.exit(3)

say("files now in cookie dir: " + ", ".join(sorted(p.name for p in COOKIE_DIR.iterdir())))
say("DONE")
