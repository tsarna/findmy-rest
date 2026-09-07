"""The 2FA submission window.

An SMS webhook wired straight to /login/2fa is a replay target: VoIP.ms
redelivers events, and every bad submission burns an attempt against the
account. The service therefore only accepts a code it asked for, recently, once.
"""

from __future__ import annotations

import pytest

from findmy_rest.config import Settings
from findmy_rest.session import AppleSession, AuthRequiredError


class FakeSmsMethod:
    def __init__(self) -> None:
        self.requested = False
        self.submitted: list[str] = []

    async def request(self) -> None:
        self.requested = True

    async def submit(self, code: str) -> str:
        self.submitted.append(code)
        return "LoginState.LOGGED_IN"


class FakeAccount:
    def __init__(self, method) -> None:
        self._method = method

    async def get_2fa_methods(self):
        return [self._method]

    def to_json(self, path=None):
        return {}


def _session(tmp_path, **kw) -> tuple[AppleSession, FakeSmsMethod]:
    settings = Settings(state_dir=tmp_path, **kw)
    session = AppleSession(settings)
    method = FakeSmsMethod()
    session._account = FakeAccount(method)
    return session, method


async def test_submit_without_a_request_is_refused(tmp_path):
    session, method = _session(tmp_path)
    with pytest.raises(AuthRequiredError, match="no 2FA request is in flight"):
        await session.submit_2fa("123456")
    assert method.submitted == []


async def test_submit_after_a_request_is_accepted(tmp_path):
    session, method = _session(tmp_path)
    await session.request_2fa()
    await session.submit_2fa("123456")
    assert method.submitted == ["123456"]


async def test_a_code_is_single_use(tmp_path):
    """A redelivered webhook must not resubmit the same code."""
    session, method = _session(tmp_path)
    await session.request_2fa()
    await session.submit_2fa("123456")

    with pytest.raises(AuthRequiredError, match="no 2FA request is in flight"):
        await session.submit_2fa("123456")
    assert method.submitted == ["123456"]


async def test_expired_request_is_refused(tmp_path, monkeypatch):
    session, method = _session(tmp_path, two_factor_window_s=60)
    await session.request_2fa()

    import findmy_rest.session as session_module

    later = session._2fa_requested_at + 61
    monkeypatch.setattr(session_module.time, "monotonic", lambda: later)

    with pytest.raises(AuthRequiredError, match="expired"):
        await session.submit_2fa("123456")
    assert method.submitted == []


async def test_pending_flag_tracks_the_window(tmp_path, monkeypatch):
    session, _ = _session(tmp_path, two_factor_window_s=60)
    assert session.two_factor_pending is False

    await session.request_2fa()
    assert session.two_factor_pending is True

    import findmy_rest.session as session_module

    later = session._2fa_requested_at + 61
    monkeypatch.setattr(session_module.time, "monotonic", lambda: later)
    assert session.two_factor_pending is False
