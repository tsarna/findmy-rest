"""Tests for the parts that don't need Apple: mapping, filtering, serialization."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from findmy_rest.app import create_app, parse_duration
from findmy_rest.config import Settings
from findmy_rest.identity import slugify, valid_slug
from findmy_rest.models import Device, Kind, Source, battery_from_status


def test_battery_from_status_uses_top_two_bits():
    # FindMy.py reads bits 6-7 of the status byte.
    assert battery_from_status(0b00000000) == "full"
    assert battery_from_status(0b01000000) == "medium"
    assert battery_from_status(0b10000000) == "low"
    assert battery_from_status(0b11000000) == "critical"
    # Low bits carry other meaning and must not disturb the reading.
    assert battery_from_status(0b00111111) == "full"
    assert battery_from_status(None) is None


def test_battery_only_device_is_valid():
    """An FMIP device whose owner doesn't share location: normal, not an error."""
    device = Device(
        upstream_id="x",
        display_name="Her iPhone",
        kind=Kind.IDEVICE,
        source=Source.FMIP,
        owner="jane",
        device="phone",
        battery_pct=64,
    )
    assert device.has_location is False
    assert device.location_age_s is None

    wire = device.serialize()
    assert wire["has_location"] is False
    assert wire["battery_pct"] == 64
    assert "lat" not in wire  # exclude_none keeps the payload honest


def test_serialize_materialises_computed_fields():
    device = Device(
        upstream_id="k",
        display_name="Keys",
        kind=Kind.ACCESSORY,
        source=Source.FINDMY,
        owner="jane",
        device="keys",
        lat=42.0,
        lon=-71.0,
        time=datetime.now(tz=timezone.utc) - timedelta(minutes=5),
    )
    wire = device.serialize()
    assert wire["has_location"] is True
    assert 290 < wire["location_age_s"] < 310


def test_naive_timestamps_are_treated_as_utc():
    device = Device(
        upstream_id="k",
        display_name="Keys",
        kind=Kind.ACCESSORY,
        source=Source.FINDMY,
        owner="jane",
        device="keys",
        lat=1.0,
        lon=2.0,
        time=datetime.utcnow() - timedelta(minutes=1),  # noqa: DTZ003 - deliberately naive
    )
    assert device.location_age_s is not None
    assert 50 < device.location_age_s < 70


@pytest.mark.parametrize(
    ("value", "expected"),
    [("900", 900), ("30s", 30), ("15m", 900), ("2h", 7200), ("1d", 86400), (None, None)],
)
def test_parse_duration(value, expected):
    assert parse_duration(value) == expected


def test_parse_duration_rejects_nonsense():
    from fastapi import HTTPException

    with pytest.raises(HTTPException):
        parse_duration("soon")


def test_exclude_is_case_insensitive_substring():
    settings = Settings(exclude=("old macbook", "Retired"))
    assert settings.is_excluded("Old MacBook Pro") is True
    assert settings.is_excluded("retired ipad") is True
    assert settings.is_excluded("John's Keys") is False


def _client(tmp_path):
    settings = Settings(state_dir=tmp_path / "state", keys_dir=tmp_path / "keys")
    return TestClient(create_app(settings))


def test_health_reports_auth_required_without_crashing(tmp_path):
    """No session and no keys is a normal cold start, not a failure."""
    with _client(tmp_path) as client:
        response = client.get("/health")
        assert response.status_code == 200
        body = response.json()
        assert body["ok"] is True
        assert body["findmy"]["enabled"] is False


def test_devices_empty_when_no_keys(tmp_path):
    with _client(tmp_path) as client:
        assert client.get("/devices").json() == []


def test_devices_rejects_bad_max_age(tmp_path):
    with _client(tmp_path) as client:
        assert client.get("/devices", params={"max_age": "whenever"}).status_code == 400


def test_exclude_matches_model_not_just_name():
    """AirPods report as 'Case'/'left'/'right' -- only the model identifies them."""
    settings = Settings(exclude=("AirPods",))
    assert settings.is_excluded("Case", "AirPods 4") is True
    assert settings.is_excluded("left", "AirPods 4") is True
    assert settings.is_excluded("John's Keys", "") is False


def test_openapi_schema_generates(tmp_path):
    """Request models must be resolvable, or /docs and /openapi.json 500."""
    settings = Settings(state_dir=tmp_path / "state", keys_dir=tmp_path / "keys")
    schema = create_app(settings).openapi()
    assert "/devices" in schema["paths"]
    assert "/login/2fa" in schema["paths"]


# ── identity: slugs safe for MQTT topics, filenames and labels ────────────────


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("John's Keys", "johns-keys"),
        ("Ty\u2019s Apple\xa0Watch", "tys-apple-watch"),  # curly quote + nbsp
        ("Kayak \U0001f42c", "kayak"),  # emoji dropped, not transliterated
        ("  ---  ", "unknown"),  # nothing usable left
        ("", "unknown"),
        (None, "unknown"),
    ],
)
def test_slugify(raw, expected):
    assert slugify(raw) == expected


def test_slug_never_contains_mqtt_wildcards():
    """Apple identifiers contain '#' and '/', which would break topic routing."""
    hostile = "a:/47523436-374B~#\u00b6\u00a7\u00a7-Case #2 + more"
    slug = slugify(hostile)
    assert valid_slug(slug)
    assert not set(slug) & set("#+/ ")


def test_filename_convention_gives_owner_and_device():
    settings = Settings()
    assert settings.identify(name="Keys", filename_stem="jane.keys") == ("jane", "keys")


def test_filename_without_owner_half_falls_back():
    """A bare 'keys.json' names no owner, so the default owner applies."""
    settings = Settings(default_owner="family")
    assert settings.identify(name="Keys", filename_stem="keys") == ("family", "keys")


def test_registry_overrides_filename():
    settings = Settings(_registry={"John's Keys": "jane/keys"})
    assert settings.identify(name="John's Keys", filename_stem="dad.spare") == ("jane", "keys")


def test_registry_can_match_apple_id():
    """The only handle on an iCloud device that has been renamed."""
    settings = Settings(_registry={"2006~#0064": "jane/wallet"})
    assert settings.identify(name="Anything", apple_id="2006~#0064") == ("jane", "wallet")


def test_bad_registry_value_falls_back_without_crashing():
    settings = Settings(default_owner="family", _registry={"Keys": "no-slash-here"})
    owner, device = settings.identify(name="Keys")
    assert (owner, device) == ("family", "keys")


def test_registry_value_with_unsafe_slug_is_cleaned():
    settings = Settings(_registry={"Keys": "Jane's/Keys #1"})
    owner, device = settings.identify(name="Keys")
    assert valid_slug(owner) and valid_slug(device)
    assert (owner, device) == ("janes", "keys-1")


def _device(**kw):
    base = {
        "upstream_id": "2006~#0064",
        "display_name": "John’s Keys",
        "kind": Kind.ACCESSORY,
        "source": Source.FINDMY,
        "owner": "jane",
        "device": "keys",
    }
    return Device(**{**base, **kw})


def test_id_is_the_addressable_slug():
    """`id` is what consumers key on, and it is the owner/device pair."""
    wire = _device().serialize()
    assert wire["id"] == "jane/keys"
    # The halves stay on the wire: a consumer building `findmy/jane/keys/...`
    # should not have to split the id back apart.
    assert (wire["owner"], wire["device"]) == ("jane", "keys")


def test_id_cannot_be_overridden_to_disagree_with_its_halves():
    """Derived, not passed in, so no caller can desynchronise the two."""
    assert _device(id="something/else").id == "jane/keys"


def test_apple_identifier_is_carried_but_not_as_id():
    """Apple's string is opaque and hostile to topics; it must not be `id`."""
    wire = _device().serialize()
    assert wire["upstream_id"] == "2006~#0064"
    assert "#" not in wire["id"]


def test_display_name_carries_apples_freeform_name():
    assert _device().serialize()["display_name"] == "John’s Keys"


def test_apostrophe_variants_slug_identically():
    """Apple writes names with a curly apostrophe; humans type a straight one."""
    assert slugify("John's Keys") == slugify("John’s Keys") == "johns-keys"


# ── background refresh ────────────────────────────────────────────────────────


async def test_devices_returns_promptly_while_a_slow_fetch_runs(tmp_path, monkeypatch):
    """A silent accessory costs ~38s of key derivation on every fetch, and that
    must never become request latency for a five-minute poller."""
    import asyncio

    from findmy_rest.backends.accessories import AccessoryBackend

    settings = Settings(state_dir=tmp_path / "state", keys_dir=tmp_path / "keys")
    backend = AccessoryBackend(settings, object())
    backend._accessories = ["pretend-accessory"]

    started = asyncio.Event()
    release = asyncio.Event()

    async def slow_fetch():
        started.set()
        await release.wait()
        backend._cache = ["fresh"]
        return backend._cache

    monkeypatch.setattr(backend, "_do_fetch", slow_fetch)

    # First call kicks the refresh off and returns immediately with what it has.
    assert await backend.fetch() == []
    await asyncio.wait_for(started.wait(), timeout=1)

    # A second call while the first is still running must not start another.
    task = backend._refresh
    assert await backend.fetch() == []
    assert backend._refresh is task

    release.set()
    await task
    assert await backend.fetch() == ["fresh"]


async def test_wait_blocks_for_the_refresh(tmp_path, monkeypatch):
    from findmy_rest.backends.accessories import AccessoryBackend

    settings = Settings(state_dir=tmp_path / "state", keys_dir=tmp_path / "keys")
    backend = AccessoryBackend(settings, object())
    backend._accessories = ["pretend-accessory"]

    async def quick_fetch():
        backend._cache = ["fresh"]
        return backend._cache

    monkeypatch.setattr(backend, "_do_fetch", quick_fetch)
    assert await backend.fetch(wait=True) == ["fresh"]


# ── battery: one series across both backends ──────────────────────────────────


def _battery(**kw):
    return Device(
        upstream_id="x",
        display_name="n",
        kind=Kind.ACCESSORY,
        source=Source.FINDMY,
        owner="o",
        device="d",
        **kw,
    ).serialize()


def test_accessory_level_gains_an_estimated_percentage():
    """Accessories report four levels and never a percentage; a dashboard wants
    one series for every device."""
    wire = _battery(battery_level="critical")
    assert wire["battery_pct"] == 10  # where iOS/macOS turn the indicator red
    assert wire["battery_estimated"] is True


def test_measured_percentage_is_never_marked_estimated():
    wire = _battery(battery_pct=64)
    assert wire["battery_pct"] == 64
    assert wire.get("battery_estimated", False) is False


@pytest.mark.parametrize(
    ("pct", "level"),
    [
        (100, "full"),
        (85, "full"),  # boundaries round up
        (84, "medium"),
        (64, "medium"),
        (55, "medium"),
        (54, "low"),
        (25, "low"),
        (24, "critical"),
        (0, "critical"),
    ],
)
def test_percentage_takes_the_nearest_level(pct, level):
    """Cut points are the midpoints between the stand-in values. Cutting at the
    stand-ins themselves would make a device `full` only at exactly 100%."""
    assert _battery(battery_pct=pct)["battery_level"] == level


@pytest.mark.parametrize("level", ["full", "medium", "low", "critical"])
def test_level_survives_a_round_trip_through_a_percentage(level):
    """Otherwise an accessory's own level and its plotted value disagree."""
    pct = _battery(battery_level=level)["battery_pct"]
    assert _battery(battery_pct=pct)["battery_level"] == level


def test_a_backend_supplying_both_is_left_alone():
    wire = _battery(battery_level="full", battery_pct=12)
    assert (wire["battery_level"], wire["battery_pct"]) == ("full", 12)
    assert wire.get("battery_estimated", False) is False


def test_device_type_uses_bits_five_and_four():
    """The same byte carries battery in 7-6 and device type in 5-4."""
    from findmy_rest.models import device_type_from_status

    assert device_type_from_status(0b00000000) == "apple_device"
    assert device_type_from_status(0b00010000) == "airtag"
    assert device_type_from_status(0b00100000) == "third_party"
    assert device_type_from_status(0b00110000) == "airpods"
    # The battery bits above and the unknown bits below must not disturb it.
    assert device_type_from_status(0b11011111) == "airtag"
    assert device_type_from_status(None) is None


def test_kind_follows_the_thing_not_the_backend():
    """An iPhone reached through exported keys is still an idevice.

    It beacons only while offline, but that describes how it was observed, which
    is what `source` records.
    """
    from findmy_rest.models import Kind, kind_from_device_type

    assert kind_from_device_type("apple_device") is Kind.IDEVICE
    assert kind_from_device_type("airtag") is Kind.ACCESSORY
    assert kind_from_device_type("third_party") is Kind.ACCESSORY
    assert kind_from_device_type("airpods") is Kind.ACCESSORY
    # Unknown must not resolve to a guess: the caller leaves `kind` alone, and a
    # `kind` that flaps mints a second metric series for one device.
    assert kind_from_device_type(None) is None


def test_device_type_survives_a_poll_with_no_report():
    """Accessories report intermittently; `kind` must not flap in the gaps.

    Reintroduce the bug by dropping the carry-forward in
    AccessoryBackend._remember_device_type and this fails: the device reverts to
    the provisional `accessory`, and every quiet cycle mints a second series.
    """
    from findmy_rest.backends.accessories import AccessoryBackend
    from findmy_rest.models import Device, Kind, Source

    def _phone(kind=Kind.ACCESSORY, **kw):
        # kind defaults to the provisional value the backend assigns before any
        # report has been decrypted.
        return Device(
            upstream_id="u",
            display_name="TPhone",
            kind=kind,
            source=Source.FINDMY,
            owner="tsarna",
            device="phone",
            **kw,
        )

    known = _phone(device_type="apple_device", kind=Kind.IDEVICE)
    fresh = _phone()  # this cycle decrypted nothing, so no status byte

    carried = AccessoryBackend._remember_device_type(fresh, known)
    assert carried.device_type == "apple_device"
    assert carried.kind is Kind.IDEVICE

    # A report this cycle wins over what we remembered.
    reported = _phone(device_type="airtag", kind=Kind.ACCESSORY)
    assert AccessoryBackend._remember_device_type(reported, known).kind is Kind.ACCESSORY

    # Nothing known anywhere: leave the provisional value rather than inventing one.
    assert AccessoryBackend._remember_device_type(_phone(), None).device_type is None


def test_fmip_has_its_own_fetch_floor(monkeypatch):
    """The two backends must not share an interval.

    The accessory path reads reports Apple already holds; an FMIP fetch asks Apple
    to *locate* real devices, reaching out to them. Sharing `min_fetch_interval_s`
    would poll both at whatever suits the cheap one -- 5 minutes in this
    deployment, which is far more attention than a phone's position needs.
    """
    from findmy_rest.config import Settings

    settings = Settings()
    assert settings.fmip_min_fetch_interval_s == 900
    assert settings.fmip_min_fetch_interval_s > settings.min_fetch_interval_s

    monkeypatch.setenv("FINDMY_REST_FMIP_MIN_FETCH_INTERVAL", "1800")
    assert Settings.from_env().fmip_min_fetch_interval_s == 1800
