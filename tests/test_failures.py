"""What happens after a failed fetch.

From 2026-09-14 the iCloud backend failed every poll and retried a full Apple
login on every request -- ~287 failed logins a day for five weeks -- while
/health said ok with auth_required false. These pin the fixes: a failure counts
against the floor, repeated failures back off, auth failures are recognised by
type and surfaced, and a dead-but-LOGGED_IN session can be replaced by /login.
"""

from __future__ import annotations

from pathlib import Path
from typing import ClassVar

import pytest
from fastapi.testclient import TestClient
from findmy.errors import InvalidCredentialsError, UnauthorizedError, UnhandledProtocolError
from findmy.reports import LoginState

from findmy_rest import memory
from findmy_rest import session as session_mod
from findmy_rest.app import create_app
from findmy_rest.backends.accessories import AccessoryBackend
from findmy_rest.backends.idevices import FmipBackend
from findmy_rest.config import Settings
from findmy_rest.failures import MAX_BACKOFF_S, Backoff, is_auth_failure
from findmy_rest.session import AppleSession, AuthRequiredError

# ── classification ────────────────────────────────────────────────────────────


# Stand-ins named like pyicloud's, so the test needs no optional dependency --
# the same reason the classifier matches on names.
class PyiCloudAcceptTermsException(Exception):
    pass


class PyiCloudAuthRequiredException(Exception):
    pass


class PyiCloudFailedLoginException(Exception):
    pass


class SubclassOfAuth(PyiCloudFailedLoginException):
    pass


@pytest.mark.parametrize(
    "exc",
    [
        # The exact failures of 2026-09-14 .. 2026-10-24, none of which said "2fa"
        # or "login" in their message.
        PyiCloudAcceptTermsException("You must accept the updated terms of service"),
        PyiCloudAuthRequiredException("Re-authentication required for account"),
        PyiCloudFailedLoginException("Invalid email/password combination."),
        UnhandledProtocolError("Error response for GSA request: 503"),
        InvalidCredentialsError("Email verification failed: This action cannot be completed"),
        UnauthorizedError("401"),
        AuthRequiredError("no logged-in Apple session"),
        SubclassOfAuth("matched through the MRO"),
    ],
)
def test_auth_failures_are_recognised_by_type(exc):
    assert is_auth_failure(exc)


@pytest.mark.parametrize("exc", [RuntimeError("login"), TimeoutError(), ValueError("2fa")])
def test_other_failures_are_transient_whatever_they_say(exc):
    # The old check matched these on their text.
    assert not is_auth_failure(exc)


# ── backoff ───────────────────────────────────────────────────────────────────


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


def test_backoff_doubles_from_the_floor_and_caps():
    clock = Clock()
    backoff = Backoff(clock=clock)
    assert backoff.ready()
    assert backoff.retry_in_s() is None

    delays = [backoff.failed(900, auth=False) for _ in range(8)]
    assert delays[:5] == [900, 1800, 3600, 7200, 14400]
    assert max(delays) == MAX_BACKOFF_S
    assert not backoff.ready()

    clock.now += MAX_BACKOFF_S
    assert backoff.ready()


def test_auth_failure_goes_straight_to_the_cap():
    backoff = Backoff(clock=Clock())
    assert backoff.failed(60, auth=True) == MAX_BACKOFF_S


def test_success_resets_the_backoff():
    backoff = Backoff(clock=Clock())
    backoff.failed(60, auth=True)
    backoff.succeeded()
    assert backoff.ready()
    assert backoff.failures == 0
    assert backoff.retry_in_s() is None


# ── FMIP backend ──────────────────────────────────────────────────────────────


def _fmip(tmp_path: Path, exc: BaseException) -> tuple[FmipBackend, list[int]]:
    settings = Settings(state_dir=tmp_path, enable_fmip=True)
    backend = FmipBackend(settings)
    calls: list[int] = []

    def failing():
        calls.append(1)
        raise exc

    backend._fetch_blocking = failing
    return backend, calls


async def test_fmip_failure_respects_the_floor(tmp_path):
    """The bug: nothing cached meant the floor never applied."""
    backend, calls = _fmip(tmp_path, TimeoutError("network"))
    for _ in range(20):
        assert await backend.fetch() == []
    assert len(calls) == 1
    assert backend.health.auth_required is False
    assert backend.backoff.failures == 1


async def test_fmip_terms_failure_flags_auth_and_backs_off_to_the_cap(tmp_path):
    backend, calls = _fmip(tmp_path, PyiCloudAcceptTermsException("accept the terms"))
    for _ in range(5):
        await backend.fetch()
    assert len(calls) == 1
    assert backend.health.auth_required is True
    assert "PyiCloudAcceptTermsException" in backend.health.last_error
    assert backend.backoff.retry_in_s() > MAX_BACKOFF_S - 60


async def test_fmip_force_overrides_the_backoff(tmp_path):
    backend, calls = _fmip(tmp_path, PyiCloudAcceptTermsException("accept the terms"))
    await backend.fetch()
    await backend.fetch(force=True)
    assert len(calls) == 2


async def test_fmip_success_clears_the_backoff(tmp_path):
    backend, _ = _fmip(tmp_path, TimeoutError())
    await backend.fetch()
    backend._fetch_blocking = list  # succeeds with no devices
    await backend.fetch(force=True)
    assert backend.backoff.failures == 0
    assert backend.health.last_error is None


# ── accessory backend ─────────────────────────────────────────────────────────


class FakeAccessory:
    name = "keys"
    identifier = "keys"
    model = None

    def get_min_index(self, dt):
        return 0

    def get_max_index(self, dt):
        return 100


class FailingAccount:
    def __init__(self, exc: BaseException) -> None:
        self.exc = exc
        self.calls = 0

    async def fetch_location(self, selected):
        self.calls += 1
        raise self.exc


class FakeSession:
    def __init__(self, account) -> None:
        self._account_obj = account
        self.auth_required = False

    async def account(self):
        return self._account_obj


def _accessories(tmp_path: Path, exc: BaseException):
    settings = Settings(state_dir=tmp_path, keys_dir=tmp_path / "keys")
    account = FailingAccount(exc)
    session = FakeSession(account)
    backend = AccessoryBackend(settings, session)
    backend._accessories = [FakeAccessory()]
    return backend, account, session


async def test_accessory_failure_respects_the_floor(tmp_path):
    backend, account, _ = _accessories(tmp_path, TimeoutError())
    for _ in range(10):
        await backend.fetch(wait=True)
    assert account.calls == 1


async def test_accessory_gsa_refusal_flags_auth_on_backend_and_session(tmp_path):
    backend, account, session = _accessories(
        tmp_path, UnhandledProtocolError("Error response for GSA request: 503")
    )
    await backend.fetch(wait=True)
    await backend.fetch(wait=True)
    assert account.calls == 1
    assert backend.health.auth_required is True
    assert session.auth_required is True
    assert backend.backoff.retry_in_s() > MAX_BACKOFF_S - 60


async def test_reauthenticating_lifts_the_backoff(tmp_path):
    backend, account, _ = _accessories(tmp_path, UnauthorizedError("401"))
    await backend.fetch(wait=True)
    backend.reauthenticated()
    await backend.fetch(wait=True)
    assert account.calls == 2


async def test_memory_is_trimmed_after_a_walk_even_when_it_fails(tmp_path, monkeypatch):
    trims: list[int] = []
    monkeypatch.setattr(memory, "trim", lambda: trims.append(1))
    backend, _, _ = _accessories(tmp_path, TimeoutError())
    await backend.fetch(wait=True)
    assert trims == [1]


def test_trim_is_safe_to_call_anywhere():
    # A no-op off glibc (this suite also runs on macOS); must never raise.
    assert memory.trim() is None or memory.trim() >= 0


# ── /login on a dead-but-LOGGED_IN session ────────────────────────────────────


class StaleAccount:
    login_state = LoginState.LOGGED_IN

    def __init__(self) -> None:
        self.closed = False

    async def close(self) -> None:
        self.closed = True

    async def login(self, username, password):
        raise AssertionError("must not log in on the stale account")

    def to_json(self, path=None):
        return {}


class FreshAccount:
    created: ClassVar[list[FreshAccount]] = []

    def __init__(self, anisette) -> None:
        self.anisette = anisette
        self.login_state = LoginState.LOGGED_OUT
        FreshAccount.created.append(self)

    async def login(self, username, password):
        self.login_state = LoginState.LOGGED_IN
        return LoginState.LOGGED_IN

    def to_json(self, path=None):
        return {}


class SavedIdentity:
    def to_json(self, path=None):
        return {}


async def test_login_replaces_a_stale_session_but_keeps_the_identity(tmp_path, monkeypatch):
    FreshAccount.created = []
    monkeypatch.setattr(session_mod, "AsyncAppleAccount", FreshAccount)
    session = AppleSession(Settings(state_dir=tmp_path, apple_id="a@b.c", password="pw"))
    identity = SavedIdentity()
    monkeypatch.setattr(session, "_build_anisette", lambda: identity)
    stale = StaleAccount()
    session._account = stale

    result = await session.login()

    assert result["state"] == str(LoginState.LOGGED_IN)
    assert stale.closed
    assert len(FreshAccount.created) == 1
    # Same saved anisette identity, not a newly provisioned device.
    assert FreshAccount.created[0].anisette is identity
    assert session.auth_required is False


# ── /health ───────────────────────────────────────────────────────────────────


def test_health_reports_ages_and_session_auth(tmp_path):
    settings = Settings(state_dir=tmp_path / "state", keys_dir=tmp_path / "keys", enable_fmip=True)
    with TestClient(create_app(settings)) as client:
        body = client.get("/health").json()
    assert body["ok"] is True
    # Enabled but never succeeded: a number counted from start, not null.
    assert isinstance(body["fmip"]["last_success_age_s"], float)
    assert body["fmip"]["retry_in_s"] is None
    # No saved session is a login-required state, before any fetch has run.
    assert body["findmy"]["auth_required"] is True
    # The accessory backend is off here (no keys), so it has no age.
    assert body["findmy"]["last_success_age_s"] is None
