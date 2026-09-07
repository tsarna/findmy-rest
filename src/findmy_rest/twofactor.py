"""Choosing a 2FA delivery method without a human present.

An index alone is a poor handle: Apple's list order is not contractual, and the
automation account has no trusted device at all, so index 0 is the wrong answer
there. `FINDMY_REST_2FA_PREFER` accepts, in order of usefulness:

    sms:5537        the SMS method whose number ends 5537 -- survives reordering
    sms             the first SMS method
    trusted-device  the first non-SMS method
    1               a bare index, as an escape hatch

Apple masks the number ("+1 ... .. .. 37"), so only the trailing digits are
usable for matching; a suffix shorter than the mask still works.
"""

from __future__ import annotations

import logging
import re

logger = logging.getLogger(__name__)


class NoSuchMethodError(RuntimeError):
    """The configured preference matched none of the offered methods."""


def _digits(value: object) -> str:
    return re.sub(r"\D", "", str(value or ""))


def is_sms(method: object) -> bool:
    # Type name is the reliable signal; a phone number is corroborating.
    return (
        "sms" in type(method).__name__.lower() or getattr(method, "phone_number", None) is not None
    )


def describe(methods: list) -> str:
    parts = []
    for i, method in enumerate(methods):
        phone = getattr(method, "phone_number", None)
        parts.append(f"[{i}] {type(method).__name__}" + (f" {phone}" if phone else ""))
    return ", ".join(parts) or "(none offered)"


def select(methods: list, preference: str | None) -> int:
    """Resolve a preference to an index into `methods`."""
    if not methods:
        msg = "Apple offered no 2FA methods"
        raise NoSuchMethodError(msg)

    pref = (preference or "").strip().lower()
    if not pref:
        return 0

    if pref.isdigit():
        index = int(pref)
        if index >= len(methods):
            msg = f"2FA method index {index} out of range; offered: {describe(methods)}"
            raise NoSuchMethodError(msg)
        return index

    if pref in {"trusted-device", "trusted_device", "td", "device"}:
        for i, method in enumerate(methods):
            if not is_sms(method):
                return i
        msg = f"no trusted-device 2FA method offered; offered: {describe(methods)}"
        raise NoSuchMethodError(msg)

    if pref == "sms" or pref.startswith("sms:"):
        wanted = _digits(pref.partition(":")[2])
        for i, method in enumerate(methods):
            if not is_sms(method):
                continue
            if not wanted or _digits(getattr(method, "phone_number", "")).endswith(wanted):
                return i
        detail = f" ending {wanted}" if wanted else ""
        msg = f"no SMS 2FA method{detail} offered; offered: {describe(methods)}"
        raise NoSuchMethodError(msg)

    msg = f"unrecognised 2FA preference {preference!r} (try sms, sms:1234, trusted-device, or an index)"
    raise NoSuchMethodError(msg)
