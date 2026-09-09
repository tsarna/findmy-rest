"""Reconciling the two backends' views of one device.

The cases here are the states a phone actually passes through, because the merge
exists to follow it between them: online (FMIP knows where it is, the beacons have
stopped), and off or in airplane mode (it beacons, FMIP is frozen at the moment it
went offline).
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from findmy_rest.merge import merge_devices
from findmy_rest.models import Device, Kind, Source

NOW = datetime(2026, 9, 9, 12, 0, tzinfo=timezone.utc)


def _device(source: Source, *, when: datetime | None, eph: float | None = None, **kw) -> Device:
    return Device(
        upstream_id=f"upstream-{source.value}",
        display_name="TPhone",
        kind=Kind.IDEVICE,
        source=source,
        owner="tsarna",
        device="phone",
        lat=43.9548 if when else None,
        lon=-70.1029 if when else None,
        time=when,
        eph=eph,
        **kw,
    )


def test_online_phone_takes_the_icloud_fix():
    """FMIP wins while the phone is online: the beacons stopped hours ago."""
    accessory = _device(Source.FINDMY, when=NOW - timedelta(hours=15), eph=98.0)
    fmip = _device(Source.FMIP, when=NOW - timedelta(minutes=2), eph=5.3)

    (merged,) = merge_devices([accessory, fmip])

    assert merged.source is Source.FMIP
    assert merged.eph == 5.3


def test_offline_phone_takes_the_beacon_fix():
    """The accessory path wins when the phone is off -- the case that matters most.

    FMIP has nothing newer than the moment connectivity stopped, while the device
    is beaconing precisely because it is offline.
    """
    accessory = _device(Source.FINDMY, when=NOW - timedelta(minutes=8), eph=98.0)
    fmip = _device(Source.FMIP, when=NOW - timedelta(hours=6), eph=5.3)

    (merged,) = merge_devices([accessory, fmip])

    assert merged.source is Source.FINDMY
    assert merged.eph == 98.0


def test_a_located_record_beats_one_with_no_fix_at_all():
    """A device sharing battery but no position must not displace a real fix.

    This is the steady state for an iCloud device whose owner does not share
    location, so it is not an edge case.
    """
    accessory = _device(Source.FINDMY, when=NOW - timedelta(days=2))
    fmip = _device(Source.FMIP, when=None, battery_pct=84)

    # Both orderings, or the assertion passes by luck whenever the located record
    # happens to be last.
    for order in ([fmip, accessory], [accessory, fmip]):
        (merged,) = merge_devices(order)
        assert merged.source is Source.FINDMY
        assert merged.has_location


def test_devices_with_distinct_ids_all_survive():
    keys = Device(
        upstream_id="k",
        display_name="Tyler’s Keys",
        kind=Kind.ACCESSORY,
        source=Source.FINDMY,
        owner="tsarna",
        device="keys",
    )
    phone = _device(Source.FMIP, when=NOW)

    merged = merge_devices([keys, phone])

    assert sorted(d.id for d in merged) == ["tsarna/keys", "tsarna/phone"]


def test_ties_keep_the_incumbent_so_output_is_stable():
    """Equal timestamps must not reorder between polls.

    `collect` appends accessories first, so a tie leaves the backend that works
    without iCloud in place. The property that matters is determinism: a merge
    that flipped on ties would flap the `source` label every poll.
    """
    accessory = _device(Source.FINDMY, when=NOW)
    fmip = _device(Source.FMIP, when=NOW)

    assert merge_devices([accessory, fmip])[0].source is Source.FINDMY
    assert merge_devices([fmip, accessory])[0].source is Source.FMIP


def test_naive_timestamps_compare_as_utc():
    """A naive `time` must not be treated as older than everything.

    Backends normalise to UTC, but the model accepts naive datetimes, and a
    comparison that skipped them would silently favour the other backend forever.
    """
    naive = _device(Source.FMIP, when=NOW.replace(tzinfo=None))
    older = _device(Source.FINDMY, when=NOW - timedelta(hours=1))

    (merged,) = merge_devices([older, naive])

    assert merged.source is Source.FMIP


def test_nothing_located_still_yields_one_record():
    """Two battery-only views of one device collapse rather than duplicating."""
    accessory = _device(Source.FINDMY, when=None)
    fmip = _device(Source.FMIP, when=None, battery_pct=84)

    merged = merge_devices([accessory, fmip])

    assert len(merged) == 1
    assert merged[0].id == "tsarna/phone"
