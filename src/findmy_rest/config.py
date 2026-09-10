"""Configuration, entirely from the environment (12-factor, k8s-friendly)."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path

from .identity import clean_slug, from_filename, parse_registry_value, slugify


def _bool(name: str, *, default: bool = False) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    return int(raw) if raw else default


@dataclass(frozen=True)
class Settings:
    """Runtime configuration.

    Every path lives under `state_dir`, which is the PVC mount in Kubernetes: the
    Apple session, the anisette identity and its libs bundle all have to survive
    restarts, or every restart costs a password and a 2FA round trip.
    """

    apple_id: str = ""
    password: str = ""  # from a k8s Secret; only needed for the initial login

    state_dir: Path = Path(os.environ.get("FINDMY_REST_STATE_DIR", ".state"))
    keys_dir: Path = Path(os.environ.get("FINDMY_REST_KEYS_DIR", "accessories"))

    host: str = "0.0.0.0"
    port: int = 8080

    # Apple's report latency is minutes and an AirTag's key rotates every 15 minutes,
    # so fetching more often than this buys nothing and only draws attention.
    min_fetch_interval_s: int = 60

    # FMIP gets its own, much longer floor. The accessory path reads reports Apple
    # already holds, but an FMIP fetch asks Apple to *locate* real devices, which
    # reaches out to them. Sharing one interval would have the two backends polled
    # at whatever suits the cheap one -- every 5 minutes in practice, which is far
    # more attention than a phone's battery deserves for a position that barely
    # moves.
    fmip_min_fetch_interval_s: int = 900

    # Devices whose name matches any of these are dropped entirely: account cruft that
    # cannot be deleted upstream. Substring match, case-insensitive.
    exclude: tuple[str, ...] = ()

    # Key indices to derive per refresh. An accessory that reports stays aligned
    # and costs a few hundred; one that has been silent widens by ~96 a day and
    # would otherwise spend minutes of CPU per cycle looking for something that
    # is not answering. Over-budget accessories accrue credit and are polled
    # once they have saved up their cost, so the rate is inversely proportional
    # to it. 0 disables budgeting and polls everything every cycle.
    poll_budget_indices: int = 5000

    enable_fmip: bool = False

    # Which 2FA method to use unattended: "sms", "sms:5537", "trusted-device",
    # or a bare index. Empty means index 0, which is wrong for an account with
    # no trusted device.
    two_factor_prefer: str = ""

    # A submitted code is only accepted while one of our own requests is in
    # flight and younger than this. Stops replayed webhooks and stray codes
    # from burning attempts against the account.
    two_factor_window_s: int = 300

    # Owner used when nothing else identifies one. Devices land under
    # <default_owner>/<slugified-name> rather than being dropped.
    default_owner: str = "unknown"

    # Registry: {"<name or apple id>": "<owner>/<device>"}. Non-secret metadata,
    # so it belongs in a ConfigMap rather than beside the keys.
    registry_file: Path | None = None
    _registry: dict[str, str] = field(default_factory=dict, repr=False)

    @classmethod
    def from_env(cls) -> Settings:
        state_dir = Path(os.environ.get("FINDMY_REST_STATE_DIR", ".state"))

        registry_file = Path(
            os.environ.get("FINDMY_REST_REGISTRY", "/etc/findmy-rest/registry.json")
        )
        registry: dict[str, str] = {}
        if registry_file.exists():
            try:
                loaded = json.loads(registry_file.read_text())
                registry = loaded.get("devices", loaded) if isinstance(loaded, dict) else {}
            except (OSError, ValueError):
                # A broken registry costs correct topic names, not service uptime.
                registry = {}

        exclude_raw = os.environ.get("FINDMY_REST_EXCLUDE", "")
        return cls(
            apple_id=os.environ.get("FINDMY_REST_APPLE_ID", ""),
            password=os.environ.get("FINDMY_REST_PASSWORD", ""),
            state_dir=state_dir,
            keys_dir=Path(os.environ.get("FINDMY_REST_KEYS_DIR", "accessories")),
            host=os.environ.get("FINDMY_REST_HOST", "0.0.0.0"),
            port=_int("FINDMY_REST_PORT", 8080),
            min_fetch_interval_s=_int("FINDMY_REST_MIN_FETCH_INTERVAL", 60),
            fmip_min_fetch_interval_s=_int("FINDMY_REST_FMIP_MIN_FETCH_INTERVAL", 900),
            exclude=tuple(x.strip() for x in exclude_raw.split(",") if x.strip()),
            enable_fmip=_bool("FINDMY_REST_ENABLE_FMIP", default=False),
            poll_budget_indices=_int("FINDMY_REST_POLL_BUDGET", 5000),
            two_factor_prefer=os.environ.get("FINDMY_REST_2FA_PREFER", ""),
            two_factor_window_s=_int("FINDMY_REST_2FA_WINDOW", 300),
            default_owner=os.environ.get("FINDMY_REST_DEFAULT_OWNER", "unknown"),
            registry_file=registry_file if registry_file.exists() else None,
            _registry=registry,
        )

    @property
    def account_state(self) -> Path:
        return self.state_dir / "account.json"

    @property
    def anisette_state(self) -> Path:
        return self.state_dir / "anisette.json"

    @property
    def alignment_cache(self) -> Path:
        return self.state_dir / "alignment.json"

    @property
    def anisette_libs(self) -> Path:
        return self.state_dir / "anisette_libs.bin"

    def identify(
        self,
        *,
        name: str,
        apple_id: str | None = None,
        filename_stem: str | None = None,
    ) -> tuple[str, str]:
        """Resolve (owner, device) slugs. See identity.py for the precedence."""
        for key in (apple_id, name):
            if key and key in self._registry:
                parsed = parse_registry_value(
                    self._registry[key], context=f"registry entry {key!r}"
                )
                if parsed:
                    return parsed

        if filename_stem:
            parsed = from_filename(filename_stem)
            if parsed:
                return parsed

        return clean_slug(self.default_owner, context="default_owner"), slugify(name)

    def is_excluded(self, name: str, model: str | None = None) -> bool:
        """Match against name *and* model.

        Name alone is not enough: an AirPods case reports as "Case", "left" and
        "right", so the only handle on it is the model ("AirPods 4").
        """
        haystack = " ".join(part.lower() for part in (name, model) if part)
        return any(pattern.lower() in haystack for pattern in self.exclude)
