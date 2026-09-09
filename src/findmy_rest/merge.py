"""Reconciling two backends' views of one device.

An iPhone, a Watch or a Mac can be visible through both backends at once: the
accessory backend decrypts its offline-finding beacons, and the FMIP backend asks
iCloud where it is. Those are two observations of one thing, and they arrive as
two `Device` records carrying the same `id` -- so without this they would take one
bus topic and one metric label set between them, with whichever happened to be
appended last silently winning.

Choosing the *fresher fix* is not merely deduplication, it is the correct answer.
The two backends are complementary rather than redundant, and which one is right
flips with the state of the device:

  - While the device is online it reports to Apple directly and emits no beacons,
    so the accessory backend can only offer an increasingly stale fix. FMIP wins,
    and brings a much better one -- measured at ~5 m against the accessory path's
    ~98 m for the same phone.
  - While it is powered off or in airplane mode it beacons, and FMIP has nothing
    newer than the moment it went offline. The accessory backend wins, which is
    exactly the moment its location matters most.

Comparing timestamps gets both cases without either backend needing to know the
other exists.
"""

from __future__ import annotations

from collections.abc import Iterable
from datetime import timezone

from .models import Device


def _freshness(device: Device) -> tuple[int, float]:
    """Sort key: located-and-when, with "no fix at all" ordering below everything.

    A record without `time` has no location either -- both backends set the two
    together -- so this never prefers a fresh battery reading over the only
    position we have.
    """
    if device.time is None:
        return (0, 0.0)
    when = device.time if device.time.tzinfo else device.time.replace(tzinfo=timezone.utc)
    return (1, when.timestamp())


def merge_devices(devices: Iterable[Device]) -> list[Device]:
    """Collapse records sharing an `id`, keeping the freshest fix for each.

    Ties keep the incumbent, so the caller's order decides. That only arises when
    two backends report the same instant, or when neither has ever had a fix.
    Input order is otherwise preserved, which keeps output stable between polls.
    """
    best: dict[str, Device] = {}
    for device in devices:
        incumbent = best.get(device.id)
        if incumbent is None or _freshness(device) > _freshness(incumbent):
            best[device.id] = device
    return list(best.values())
