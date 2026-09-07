"""iDevice backend: phones, laptops and watches via the iCloud FMIP endpoint.

Optional, and off by default. Two reasons it is secondary: each fetch pings real
devices, and family members' devices return battery but no location unless they
share location with this account. The accessory backend is the product.

pyicloud is imported lazily so the package is not required unless enabled.
"""

from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime, timezone

from ..config import Settings
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
        self._fetched_at: float = 0.0
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
            id=apple_id,
            name=name,
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

        age = time.monotonic() - self._fetched_at
        if not force and self._cache and age < self._settings.min_fetch_interval_s:
            return self._cache

        try:
            # pyicloud is synchronous; keep it off the event loop.
            devices = await asyncio.to_thread(self._fetch_blocking)
        except Exception as exc:
            logger.exception("fmip fetch failed")
            self.health.last_error = f"{type(exc).__name__}: {exc}"
            if "2fa" in str(exc).lower() or "login" in str(exc).lower():
                self.health.auth_required = True
                self._api = None
            return self._cache

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
