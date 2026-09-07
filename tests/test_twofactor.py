"""Tests for unattended 2FA method selection.

The automation account has no trusted device, so index 0 -- the old hardcoded
default -- is the wrong answer there. Selection must survive Apple reordering
the list, and must fail loudly rather than silently picking the wrong number.
"""

from __future__ import annotations

import pytest

from findmy_rest.twofactor import NoSuchMethodError, describe, is_sms, select


class AsyncTrustedDeviceSecondFactor:
    """Named to match FindMy.py, since selection keys off the type name."""


class AsyncSmsSecondFactor:
    def __init__(self, phone_number: str) -> None:
        self.phone_number = phone_number


TD = AsyncTrustedDeviceSecondFactor()
SMS_37 = AsyncSmsSecondFactor("+1 ••• •• •• 37")
SMS_89 = AsyncSmsSecondFactor("+44 ••• •• •• 89")


def test_empty_preference_keeps_the_old_default():
    assert select([TD, SMS_37], "") == 0
    assert select([TD, SMS_37], None) == 0


def test_sms_picks_the_first_sms_method_not_index_zero():
    """The account this was built for offers SMS at index 1."""
    assert select([TD, SMS_37], "sms") == 1


def test_sms_suffix_picks_a_specific_number():
    assert select([TD, SMS_37, SMS_89], "sms:89") == 2
    assert select([TD, SMS_37, SMS_89], "sms:37") == 1


def test_suffix_matching_ignores_mask_formatting():
    """Apple masks the number; only trailing digits are usable."""
    assert select([SMS_37], "sms:37") == 0
    assert select([SMS_37], "sms: 37 ") == 0


def test_suffix_survives_reordering():
    """An index would silently select the wrong number here; a suffix does not."""
    assert select([SMS_37, SMS_89], "sms:89") == 1
    assert select([SMS_89, SMS_37], "sms:89") == 0


def test_trusted_device_preference():
    assert select([SMS_37, TD], "trusted-device") == 1


def test_bare_index_is_an_escape_hatch():
    assert select([TD, SMS_37], "1") == 1


def test_unmatched_suffix_raises_rather_than_guessing():
    with pytest.raises(NoSuchMethodError, match="ending 99"):
        select([TD, SMS_37], "sms:99")


def test_missing_trusted_device_raises():
    with pytest.raises(NoSuchMethodError, match="trusted-device"):
        select([SMS_37], "trusted-device")


def test_out_of_range_index_raises():
    with pytest.raises(NoSuchMethodError, match="out of range"):
        select([TD], "5")


def test_no_methods_at_all_raises():
    with pytest.raises(NoSuchMethodError, match="no 2FA methods"):
        select([], "sms")


def test_unrecognised_preference_raises():
    with pytest.raises(NoSuchMethodError, match="unrecognised"):
        select([TD], "carrier pigeon")


def test_is_sms_detects_both_signals():
    assert is_sms(SMS_37) is True
    assert is_sms(TD) is False


def test_describe_is_safe_for_logs_and_errors():
    text = describe([TD, SMS_37])
    assert "AsyncTrustedDeviceSecondFactor" in text
    assert "37" in text
    assert describe([]) == "(none offered)"
