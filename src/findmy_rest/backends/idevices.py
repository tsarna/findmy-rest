"""iDevice backend: phones, laptops and watches via the iCloud FMIP endpoint.

Optional and off by default, but not redundant: it is the *only* way to see an
online Apple device. A phone with any route to Apple reports directly and emits no
offline-finding beacons, so the accessory backend cannot see it at all -- measured
at 23 hours of silence from a phone sitting powered on, which resumed reporting
within ~17 minutes of going into airplane mode. When both backends can see a
device, merge.py keeps the fresher fix; in practice that is FMIP while it is online
(~5 m against ~98 m for the same phone) and the accessory path while it is off.

Two costs keep it secondary. Each fetch asks Apple to locate real devices rather
than reading reports it already holds, hence the separate, longer
`fmip_min_fetch_interval_s`. And family members' devices return battery but no
location unless that member shares location with this account -- which requires
both the per-person share *and* the account-level "Share My Location" switch. With
the master switch off the share is accepted and listed but inert, and the API
reports exactly what it reports when nothing is shared at all: `fmlyShare: false`
and no location. Nothing distinguishes the two, so check the switch first.

pyicloud is imported lazily so the package is not required unless enabled.
"""

from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime, timezone

from ..config import Settings
from ..failures import Backoff, is_auth_failure
from ..models import BackendHealth, Device, Kind, Source

logger = logging.getLogger(__name__)

# FMIP's numeric device status, as surfaced to consumers.
DEVICE_STATUS = {"200": "online", "201": "offline", "203": "pending", "204": "unregistered"}


class FmipBackend:
    source = Source.FMIP

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._api = None
        self._cache: list[Device] = []
        # Last *attempt*, successful or not: a failure counts against the floor.
        # None until the first one, so the floor never depends on host uptime.
        self._fetched_at: float | None = None
        self.backoff = Backoff()
        self.health = BackendHealth(enabled=settings.enable_fmip)

    def _connect(self):
        from pyicloud import PyiCloudService

        return PyiCloudService(
            self._settings.apple_id,
            self._settings.password,
            cookie_directory=str(self._settings.state_dir / "icloud"),
            with_family=True,
        )

    def _to_device(self, data: dict) -> Device:
        name = data.get("name") or data.get("deviceDisplayName") or "unknown"
        location = data.get("location") or {}
        level = data.get("batteryLevel")

        when = None
        stamp = location.get("timeStamp")
        if isinstance(stamp, (int, float)):
            when = datetime.fromtimestamp(stamp / 1000, tz=timezone.utc)

        apple_id = data.get("id") or name
        # No file to name these, so the registry is the only explicit source.
        owner, device_slug = self._settings.identify(name=name, apple_id=apple_id)
        return Device(
            upstream_id=apple_id,
            display_name=name,
            kind=Kind.IDEVICE,
            source=Source.FMIP,
            owner=owner,
            device=device_slug,
            lat=location.get("latitude"),
            lon=location.get("longitude"),
            time=when,
            eph=location.get("horizontalAccuracy"),
            alt=location.get("altitude"),
            epv=location.get("verticalAccuracy"),
            battery_pct=round(level * 100) if isinstance(level, (int, float)) and level else None,
            device_status=DEVICE_STATUS.get(str(data.get("deviceStatus")), None),
        )

    def _fetch_blocking(self) -> list[Device]:
        if self._api is None:
            self._api = self._connect()

        # Refresh the service token ourselves, before touching `.devices`.
        #
        # pyicloud's own recovery is the hazard. When the findme service token
        # expires, `_refresh_client` raises and
        # FindMyiPhoneServiceManager._refresh_client_with_reauth answers with
        # `authenticate(force_refresh=True)` -- throwing away a perfectly good
        # trusted session for a full SRP re-login. Apple refuses that often
        # enough to matter, and every API error during SRP is relabelled
        # "Invalid email/password combination" (base.py:701) regardless of the
        # real cause, so the failure reads as a credentials problem it is not.
        # Observed: the first fetch after a session had sat overnight died that
        # way, with the password provably correct.
        #
        # A plain authenticate() reuses the trusted session and re-runs
        # accountLogin, which is all an expired *service* token needs. Both
        # `.devices` and `refresh()` route through that recovery, so this has to
        # happen before either.
        self._api.authenticate()

        manager = self._api.devices
        manager.refresh()  # family devices arrive on a follow-up poll
        return [
            self._to_device(d.data)
            for d in manager
            if not self._settings.is_excluded(
                d.data.get("name") or "", d.data.get("deviceDisplayName")
            )
        ]

    async def fetch(self, *, force: bool = False) -> list[Device]:
        if not self._settings.enable_fmip:
            return []

        # Gated on the last attempt, not on having a cache. The old `self._cache and
        # age < floor` meant a backend that had never succeeded -- so had nothing
        # cached -- skipped the floor entirely and retried a login on every request.
        # `force` is an operator asking explicitly, so it overrides the backoff too.
        throttled = (
            self._fetched_at is not None
            and time.monotonic() - self._fetched_at < self._settings.fmip_min_fetch_interval_s
        )
        if not force and (throttled or not self.backoff.ready()):
            return self._cache

        try:
            # pyicloud is synchronous; keep it off the event loop.
            devices = await asyncio.to_thread(self._fetch_blocking)
        except Exception as exc:
            auth = is_auth_failure(exc)
            delay = self.backoff.failed(self._settings.fmip_min_fetch_interval_s, auth=auth)
            self._fetched_at = time.monotonic()
            logger.exception(
                "fmip fetch failed (%s); next attempt in %.0fs",
                "needs re-authentication" if auth else "transient",
                delay,
            )
            self.health.last_error = f"{type(exc).__name__}: {exc}"
            if auth:
                self.health.auth_required = True
                # Reconnect next time: a fresh PyiCloudService reloads the trusted
                # session from the cookie directory instead of reusing a dead one.
                self._api = None
            return self._cache

        self.backoff.succeeded()
        self._cache = devices
        self._fetched_at = time.monotonic()
        self.health.auth_required = False
        self.health.last_error = None
        self.health.device_count = len(devices)
        self.health.last_fetch = datetime.now(tz=timezone.utc)
        return devices

    @property
    def cached(self) -> list[Device]:
        return self._cache
