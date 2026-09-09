"""Accessory backend: AirTags and Find My network trackers, via FindMy.py.

This is the product. Keys come from a one-time export (see docs/key-export.md) and
are mounted read-only; this module never writes them.

All accessories are fetched in a single batched call, per Apple's expectations and
the project plan.
"""

from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

from findmy import FindMyAccessory

from ..alignment import AlignmentStore
from ..config import Settings
from ..models import (
    BackendHealth,
    Device,
    Kind,
    Source,
    battery_from_status,
    device_type_from_status,
    kind_from_device_type,
)
from ..session import AppleSession, AuthRequiredError

logger = logging.getLogger(__name__)

# FindMy.py asks Apple for the last 7 days of reports; the key walk covers the
# same window, so cost estimates must use it too.
REPORT_WINDOW_DAYS = 7


class AccessoryBackend:
    source = Source.FINDMY

    def __init__(self, settings: Settings, session: AppleSession) -> None:
        self._settings = settings
        self._session = session
        self._accessories: list[FindMyAccessory] = []
        self._stems: dict[int, str] = {}
        self._alignment = AlignmentStore(settings.alignment_cache)
        self._cache: list[Device] = []
        self._fetched_at: float = 0.0
        self._refresh: asyncio.Task | None = None
        # Accrued polling budget for accessories that cost more than one cycle.
        self._credits: dict[str, float] = {}
        self.health = BackendHealth()

    def load_keys(self) -> int:
        """Load exported accessory JSON. Bad files are skipped, not fatal."""
        keys_dir: Path = self._settings.keys_dir
        if not keys_dir.is_dir():
            logger.warning("keys dir %s does not exist; no accessories", keys_dir)
            self._accessories = []
            self._stems = {}
            return 0

        loaded: list[FindMyAccessory] = []
        stems: dict[int, str] = {}
        for path in sorted(keys_dir.glob("*.json")):
            try:
                accessory = FindMyAccessory.from_json(path)
            except Exception:
                logger.exception("could not load accessory %s", path.name)
                continue
            if self._settings.is_excluded(accessory.name or "", accessory.model):
                logger.info("excluding accessory %r by config", accessory.name)
                continue
            # The filename carries the operator's chosen <owner>.<device>, which
            # is also the SSM parameter name, so the key and its MQTT topic agree
            # by construction.
            stems[id(accessory)] = path.stem
            # Without this, an accessory whose alignment is a year stale costs
            # minutes of key derivation on every restart before any request.
            self._alignment.apply(path.stem, accessory)
            loaded.append(accessory)

        self._stems = stems
        self._accessories = loaded
        self.health.enabled = bool(loaded)
        logger.info("loaded %d accessory key file(s) from %s", len(loaded), keys_dir)
        return len(loaded)

    def _merge(self, selected: list, results) -> list[Device]:
        """Fresh results for what was polled, last known position for what was not.

        The cache is keyed by `upstream_id` rather than `id`: the lookup below has
        an accessory in hand and only Apple's identifier to match on, never the
        owner/device slug. Getting that wrong fails silently -- every deferred
        accessory would simply lose its position.
        """
        previous = {device.upstream_id: device for device in self._cache}

        devices = []
        for accessory in self._accessories:
            if accessory not in selected:
                # Not polled this cycle: keep what we last knew, so a deferred
                # accessory still appears, with location_age_s telling the truth
                # about how old its fix is.
                stale = previous.get(accessory.identifier)
                devices.append(stale if stale else self._to_device(accessory, None))
                continue

            report = results.get(accessory) if isinstance(results, dict) else results
            if isinstance(report, list):
                report = max(report, key=lambda r: r.timestamp) if report else None
            fresh = self._to_device(accessory, report)
            devices.append(self._remember_device_type(fresh, previous.get(accessory.identifier)))
        return devices

    @staticmethod
    def _remember_device_type(device: Device, prior: Device | None) -> Device:
        """Carry a known device type across a poll that decrypted no report.

        The type lives in the status byte, so it is only learned when a report
        arrives. Accessories report intermittently -- some go hours between fixes
        -- so without this, `kind` would flap between the real value and the
        default on every quiet cycle. `kind` is a metric label, and a label that
        flaps mints a second time series for one device, with the abandoned one
        frozen at its last value forever.
        """
        if device.device_type is None and prior is not None and prior.device_type is not None:
            device.device_type = prior.device_type
            device.kind = prior.kind
        return device

    def _to_device(self, accessory: FindMyAccessory, report) -> Device:
        name = accessory.name or accessory.identifier
        owner, device_slug = self._settings.identify(
            name=name,
            apple_id=accessory.identifier,
            filename_stem=self._stems.get(id(accessory)),
        )
        device = Device(
            upstream_id=accessory.identifier,
            display_name=name,
            kind=Kind.ACCESSORY,
            source=Source.FINDMY,
            owner=owner,
            device=device_slug,
        )
        if report is None:
            return device

        when = report.timestamp
        if when is not None and when.tzinfo is None:
            when = when.replace(tzinfo=timezone.utc)

        device.lat = report.latitude
        device.lon = report.longitude
        device.time = when
        device.eph = report.horizontal_accuracy
        device.status_raw = report.status
        device.confidence = report.confidence
        device.battery_level = battery_from_status(report.status)

        # The same byte says what the thing is. An iPhone or a Watch reached
        # through exported keys is an idevice, not an accessory -- it beacons only
        # while offline, but that describes how it was seen, not what it is, and
        # `source` already records the observer. Battery alerting depends on the
        # distinction: "replace this tracker's cell" and "this phone wants a
        # charger" are different messages.
        device.device_type = device_type_from_status(report.status)
        kind = kind_from_device_type(device.device_type)
        if kind is not None:
            device.kind = kind
        return device

    async def fetch(self, *, force: bool = False, wait: bool = False) -> list[Device]:
        """Return the known devices, refreshing in the background if stale.

        The refresh does not block the response. Finding a report means deriving
        every rolling key the accessory could be using, and that band widens by
        ~96 indices a day for anything that has not reported -- measured at 38s
        for one silent tracker and repeated on *every* fetch, since the index
        advances with the accessory's powered time and cannot be narrowed
        without finding it. A consumer polling every five minutes must not wear
        that as request latency.

        `wait=True` blocks for the refresh, for a caller that wants freshness
        over promptness.
        """
        if not self._accessories:
            return []

        age = time.monotonic() - self._fetched_at
        stale = force or not self._cache or age >= self._settings.min_fetch_interval_s
        if stale and (self._refresh is None or self._refresh.done()):
            self._refresh = asyncio.create_task(self._do_fetch())
        if wait and self._refresh is not None:
            await self._refresh

        return self._cache

    def _walk_cost(self, accessory: FindMyAccessory) -> int:
        """How many key indices a fetch would have to derive for this accessory.

        Exact and free: it is index arithmetic off the stored alignment, not the
        derivation itself. So the expensive work can be budgeted *before* doing
        it, rather than measured after.
        """
        now = datetime.now(tz=timezone.utc)
        try:
            lo = accessory.get_min_index(now - timedelta(days=REPORT_WINDOW_DAYS))
            hi = accessory.get_max_index(now)
        except Exception:  # noqa: BLE001 - never let scheduling break a fetch
            return 0
        return max(0, hi - lo + 1)

    def _select(self) -> tuple[list[FindMyAccessory], list[str]]:
        """Choose which accessories to fetch this cycle, within a cost budget.

        An accessory that reports stays aligned and costs a few hundred indices,
        so it is always included. One that has been silent widens by ~96 indices
        a day forever, and polling it every cycle would spend minutes of CPU
        looking for something that is not answering.

        Those accrue budget instead and are fetched once they have saved up
        their own cost, so an accessory 12x over budget is polled every 12th
        cycle. Cost is amortised rather than skipped: nothing is dropped, the
        expensive ones are simply rarer, in proportion to what they cost.
        """
        budget = self._settings.poll_budget_indices
        if budget <= 0:
            return list(self._accessories), []

        costs = {id(a): self._walk_cost(a) for a in self._accessories}
        expensive = [a for a in self._accessories if costs[id(a)] > budget]
        # The budget is global, not per accessory, so total work per cycle stays
        # bounded however many silent accessories accumulate. Each shares it, so
        # adding a second expensive one halves how often either is polled --
        # raise FINDMY_REST_POLL_BUDGET to trade CPU for freshness.
        share = budget / len(expensive) if expensive else budget

        selected: list[FindMyAccessory] = []
        deferred: list[str] = []
        for accessory in self._accessories:
            stem = self._stems.get(id(accessory)) or (accessory.name or "?")
            cost = costs[id(accessory)]
            if cost <= budget:
                selected.append(accessory)
                self._credits.pop(stem, None)
                continue

            credits = self._credits.get(stem, 0) + share
            if credits >= cost:
                self._credits[stem] = 0
                selected.append(accessory)
            else:
                self._credits[stem] = credits
                deferred.append(f"{stem} ({cost} indices, {credits:.0f}/{cost})")

        return selected, deferred

    async def _do_fetch(self) -> list[Device]:
        self.health.refreshing = True
        try:
            account = await self._session.account()
        except AuthRequiredError as exc:
            self.health.auth_required = True
            self.health.last_error = str(exc)
            self.health.refreshing = False
            return self._cache

        selected, deferred = self._select()
        if deferred:
            logger.info("deferring %d expensive accessory fetch(es): %s", len(deferred), deferred)
        self.health.deferred = len(deferred)
        if not selected:
            self.health.refreshing = False
            self._fetched_at = time.monotonic()
            return self._cache

        try:
            results = await account.fetch_location(selected)
        except Exception as exc:
            logger.exception("accessory fetch failed")
            self.health.last_error = f"{type(exc).__name__}: {exc}"
            # An expired session shows up here; flag it so /health surfaces it.
            if "auth" in str(exc).lower() or "401" in str(exc):
                self.health.auth_required = True
                self._session.auth_required = True
            self.health.refreshing = False
            return self._cache

        devices = self._merge(selected, results)

        # FindMy.py advances alignment in memory as it decrypts; persist it so
        # the next start does not repeat the walk.
        for accessory in selected:
            stem = self._stems.get(id(accessory))
            if stem:
                self._alignment.record(stem, accessory)
        self._alignment.save()

        self._cache = devices
        self._fetched_at = time.monotonic()
        self.health.refreshing = False
        self.health.auth_required = False
        self.health.last_error = None
        self.health.device_count = len(devices)
        self.health.last_fetch = datetime.now(tz=timezone.utc)
        self._session.save()  # session state moves on every successful fetch
        return devices

    @property
    def cached(self) -> list[Device]:
        return self._cache

    async def aclose(self) -> None:
        if self._refresh is not None and not self._refresh.done():
            self._refresh.cancel()
