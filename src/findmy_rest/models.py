"""The device object every backend maps onto.

Field names follow GPSD's TPV report where an equivalent exists (`lat`, `lon`,
`alt`, `time`, `eph`, `epv`), so the output drops straight into consumers that
already speak that vocabulary. Everything without a TPV equivalent keeps its own
name.

`lat`/`lon`/`time` are optional on purpose: an iCloud device whose owner does not
share location returns battery only, and that is a normal steady state rather than
an error.
"""

from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from itertools import pairwise

from pydantic import BaseModel, Field, model_validator

from . import __version__

# Bits 6-7 of an accessory's status byte, per FindMy.py's own scanner mapping.
# "Very Low" is normalised to "critical" so the vocabulary matches across backends.
BATTERY_FROM_STATUS = {0b00: "full", 0b01: "medium", 0b10: "low", 0b11: "critical"}

# Accessories report four levels, never a percentage. These stand-ins exist so a
# dashboard can plot one series for every device instead of two: 10 is where iOS
# and macOS turn the battery indicator red. There is no measurement behind them
# -- alert on the *level* for accessories and treat a plotted percentage as a
# shape. `battery_estimated` marks them.
BATTERY_PCT_FROM_LEVEL = {"full": 100, "medium": 70, "low": 40, "critical": 10}

# Going the other way, a percentage takes the level whose stand-in it is nearest,
# so the cut points are the midpoints between them: 85, 55, 25. Derived rather
# than written out, so changing a stand-in above moves its boundaries with it and
# the two mappings cannot drift apart.
_LEVELS_HIGH_TO_LOW = ["full", "medium", "low", "critical"]
BATTERY_LEVEL_CUTOFFS = [
    (
        (BATTERY_PCT_FROM_LEVEL[hi] + BATTERY_PCT_FROM_LEVEL[lo]) / 2,
        hi,
    )
    for hi, lo in pairwise(_LEVELS_HIGH_TO_LOW)
]


class Kind(str, Enum):
    ACCESSORY = "accessory"
    IDEVICE = "idevice"


class Source(str, Enum):
    FINDMY = "findmy"
    FMIP = "fmip"


class Device(BaseModel):
    # The addressable identity, `<owner>/<device>`. Both halves are slugs
    # ([a-z0-9-]), so this is safe in an MQTT topic, a filename, a URL or a
    # metric label, and it survives a rename in Find My.
    #
    # This is `id` because it is the identifier consumers should key on. The
    # field previously here -- Apple's opaque string -- is `upstream_id` below:
    # naming that one `id` invited exactly the mistake its own docs had to warn
    # against on the next line.
    #
    # Set in __init__ rather than stored, so it cannot drift from its halves.
    id: str = ""

    owner: str
    device: str

    # Apple's own identifier: opaque, and unusable as a topic segment, filename
    # or label -- it contains `#`, `/` and characters like `§`. Kept for
    # correlation against Find My and for registry lookups, never for addressing.
    #
    # `upstream_id` rather than `apple_id` because this project already uses
    # "Apple ID" for the iCloud *account* it authenticates as
    # (FINDMY_REST_APPLE_ID), which is an entirely different thing. It pairs with
    # `source`, which says which upstream produced the record.
    upstream_id: str

    # Apple's display name: freeform, may contain emoji, typographic apostrophes
    # and non-breaking spaces. For humans reading a dashboard, not for addressing.
    display_name: str

    kind: Kind
    source: Source

    # TPV-named location fields, all optional.
    lat: float | None = None
    lon: float | None = None
    alt: float | None = None
    time: datetime | None = None
    eph: float | None = Field(default=None, description="horizontal error, metres")
    epv: float | None = Field(default=None, description="vertical error, metres")

    battery_level: str | None = Field(default=None, description="full | medium | low | critical")
    battery_pct: int | None = Field(default=None, description="FMIP only; exact percentage")

    battery_estimated: bool = Field(
        default=False, description="battery_pct was derived from a coarse level, not measured"
    )

    status_raw: int | None = Field(default=None, description="FindMy.py accessory status byte")
    confidence: int | None = Field(default=None, description="FindMy.py report confidence, 1-3")
    device_status: str | None = Field(default=None, description="FMIP status, mapped")

    @model_validator(mode="after")
    def _derive_id(self) -> Device:
        """Keep `id` exactly `<owner>/<device>`.

        Derived rather than passed in, so no caller can construct a device whose
        id disagrees with the halves it is built from. The halves stay on the
        wire too: consumers building topic segments would otherwise have to split
        the id back apart.
        """
        self.id = f"{self.owner}/{self.device}"
        return self

    @property
    def has_location(self) -> bool:
        return self.lat is not None and self.lon is not None

    @property
    def location_age_s(self) -> float | None:
        if self.time is None:
            return None
        when = self.time if self.time.tzinfo else self.time.replace(tzinfo=timezone.utc)
        return (datetime.now(tz=timezone.utc) - when).total_seconds()

    def serialize(self) -> dict:
        """Wire form: computed fields materialised so consumers need no second call."""
        data = self.model_dump(mode="json", exclude_none=True)

        # Fill in whichever half of the battery picture the backend could not
        # supply, so a consumer can plot one series across both. Done here rather
        # than at construction because backends set these fields after building
        # the object.
        level, pct = self.battery_level, self.battery_pct
        if level and pct is None:
            derived = BATTERY_PCT_FROM_LEVEL.get(level)
            if derived is not None:
                data["battery_pct"] = derived
                data["battery_estimated"] = True
        elif pct is not None and not level:
            data["battery_level"] = battery_level_from_pct(pct)

        data["has_location"] = self.has_location
        age = self.location_age_s
        if age is not None:
            data["location_age_s"] = round(age, 1)
        return data


def battery_level_from_pct(pct: int | None) -> str | None:
    """Bucket an exact percentage into the accessory vocabulary.

    The inverse of BATTERY_PCT_FROM_LEVEL: each percentage takes the level whose
    stand-in value it is nearest, so 100-85 is full, 85-55 medium, 55-25 low and
    25-0 critical. Every level round-trips through a percentage unchanged, so an
    accessory's reported level and its plotted value cannot disagree.
    """
    if pct is None:
        return None
    for cutoff, level in BATTERY_LEVEL_CUTOFFS:
        if pct >= cutoff:
            return level
    return _LEVELS_HIGH_TO_LOW[-1]


def battery_from_status(status: int | None) -> str | None:
    if status is None:
        return None
    return BATTERY_FROM_STATUS.get((status >> 6) & 0b11)


class BackendHealth(BaseModel):
    enabled: bool = False
    auth_required: bool = False
    last_fetch: datetime | None = None
    last_error: str | None = None
    device_count: int = 0
    # A refresh is running now; results land in a later poll.
    refreshing: bool = False
    # Accessories skipped this cycle because their key walk exceeds the budget.
    # They are polled at a rate inversely proportional to their cost, not
    # dropped; their last known position is still served.
    deferred: int = 0


class Health(BaseModel):
    ok: bool
    # Which build is answering. Worth reporting because deployments pin a tag:
    # this is how you tell whether a rollout actually took, without shelling
    # into the pod to ask.
    version: str = __version__
    findmy: BackendHealth
    fmip: BackendHealth
    # True only while a code this service asked for is still acceptable. An SMS
    # automation should refuse to submit anything unless this is set.
    two_factor_pending: bool = False
