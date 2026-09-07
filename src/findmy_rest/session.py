"""The Apple session: one account, one anisette identity, both persisted.

Two rules drive everything here:

1. **Never mint a new anisette identity casually.** Each one looks to Apple like a
   new device logging into the account; a burst of them draws a 503 from GSA. The
   provisioning state is 7 KB and the libs bundle 2.2 MB, so both live on the PVC
   and are saved even when a login attempt fails.
2. **Never crash-loop on an auth failure.** If the session dies, we flag
   `auth_required`, keep serving the last known data, and wait for someone to POST
   to /login. A restart loop would burn 2FA attempts and achieve nothing.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

from findmy.reports import AsyncAppleAccount, LocalAnisetteProvider, LoginState

from .config import Settings
from .twofactor import NoSuchMethodError, describe, select

logger = logging.getLogger(__name__)


class AuthRequiredError(RuntimeError):
    """Raised when Apple needs interactive credentials that we do not have."""


class AppleSession:
    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._account: AsyncAppleAccount | None = None
        self._anisette: LocalAnisetteProvider | None = None
        self._lock = asyncio.Lock()
        # Cached so request_2fa and submit_2fa act on the *same* method objects:
        # Apple's ordering is not contractual, and an SMS method carries a
        # phone_number_id, so a reorder between the two calls would submit the
        # code against a different number than the one it was sent to.
        self._methods: list | None = None
        # When we last asked Apple to send a code, and to which method.
        self._2fa_requested_at: float | None = None
        self._2fa_requested_index: int | None = None
        self.auth_required = False
        self.last_error: str | None = None

    # ── state ───────────────────────────────────────────────────────────────

    def _libs_path(self) -> str | None:
        libs = self._settings.anisette_libs
        return str(libs) if libs.exists() else None

    def _build_anisette(self) -> LocalAnisetteProvider:
        state = self._settings.anisette_state
        if state.exists():
            logger.info("reusing anisette identity from %s", state)
            return LocalAnisetteProvider.from_json(state, libs_path=self._libs_path())
        logger.warning("no saved anisette identity; provisioning a new virtual device")
        return LocalAnisetteProvider(libs_path=self._libs_path())

    def save(self) -> None:
        """Persist session and anisette state. Safe to call often; cheap."""
        self._settings.state_dir.mkdir(parents=True, exist_ok=True)
        try:
            if self._anisette is not None:
                self._anisette.to_json(self._settings.anisette_state)
            if self._account is not None:
                self._account.to_json(self._settings.account_state)
        except OSError:
            logger.exception("could not persist session state")

    async def load(self) -> None:
        """Restore a session from disk at startup, if there is one."""
        self._settings.state_dir.mkdir(parents=True, exist_ok=True)
        state = self._settings.account_state
        if not state.exists():
            logger.info("no saved account state; login required")
            self.auth_required = True
            return

        try:
            self._account = AsyncAppleAccount.from_json(state, anisette_libs_path=self._libs_path())
        except Exception as exc:
            logger.exception("could not restore account state")
            self.last_error = f"restore failed: {exc}"
            self.auth_required = True
            return

        if self._account.login_state == LoginState.LOGGED_IN:
            logger.info("restored logged-in session for %s", self._settings.apple_id)
            self.auth_required = False
        else:
            logger.warning("restored session is %s; login required", self._account.login_state)
            self.auth_required = True

    # ── login ───────────────────────────────────────────────────────────────

    async def account(self) -> AsyncAppleAccount:
        if self._account is None or self._account.login_state != LoginState.LOGGED_IN:
            raise AuthRequiredError("no logged-in Apple session")
        return self._account

    async def login(self, password: str | None = None) -> dict[str, Any]:
        """Begin a login. Returns the available 2FA methods, if any are needed."""
        async with self._lock:
            settings = self._settings
            secret = password or settings.password
            if not settings.apple_id or not secret:
                raise AuthRequiredError("apple_id and password must be configured")

            if self._account is None:
                self._anisette = self._build_anisette()
                self._account = AsyncAppleAccount(self._anisette)
                self.save()  # keep the identity even if the login below fails

            try:
                state = await self._account.login(settings.apple_id, secret)
            finally:
                self.save()

            if state == LoginState.LOGGED_IN:
                self.auth_required = False
                self.last_error = None
                return {"state": str(state), "methods": []}

            if state == LoginState.REQUIRE_2FA:
                methods = await self._load_methods(refresh=True)
                try:
                    default = select(methods, self._settings.two_factor_prefer)
                except NoSuchMethodError as exc:
                    logger.warning("2FA preference did not match: %s", exc)
                    default = None
                return {
                    "state": str(state),
                    "selected": default,
                    "preference": self._settings.two_factor_prefer or None,
                    "methods": [
                        {
                            "index": i,
                            "type": type(m).__name__,
                            "phone_number": getattr(m, "phone_number", None),
                        }
                        for i, m in enumerate(methods)
                    ],
                }

            self.last_error = f"unexpected login state: {state}"
            raise AuthRequiredError(self.last_error)

    async def _load_methods(self, *, refresh: bool = False) -> list:
        if self._account is None:
            raise AuthRequiredError("login has not been started")
        if refresh or self._methods is None:
            self._methods = list(await self._account.get_2fa_methods())
        if not self._methods:
            raise AuthRequiredError("Apple offered no 2FA methods")
        return self._methods

    def _resolve(self, methods: list, method_index: int | None) -> int:
        if method_index is not None:
            if method_index >= len(methods):
                raise AuthRequiredError(
                    f"2FA method index {method_index} out of range; offered: {describe(methods)}"
                )
            return method_index
        try:
            return select(methods, self._settings.two_factor_prefer)
        except NoSuchMethodError as exc:
            raise AuthRequiredError(str(exc)) from exc

    async def request_2fa(self, method_index: int | None = None) -> int:
        async with self._lock:
            methods = await self._load_methods()
            index = self._resolve(methods, method_index)
            method = methods[index]
            logger.info("requesting 2FA via %s", describe([method]))
            await method.request()
            self._2fa_requested_at = time.monotonic()
            self._2fa_requested_index = index
            return index

    @property
    def two_factor_pending(self) -> bool:
        """True while a code we asked for is still acceptable."""
        if self._2fa_requested_at is None:
            return False
        age = time.monotonic() - self._2fa_requested_at
        return age <= self._settings.two_factor_window_s

    async def submit_2fa(self, code: str, method_index: int | None = None) -> str:
        async with self._lock:
            # Only accept a code we asked for, recently. An automation wired to
            # an SMS webhook must not be able to submit a replayed or unrelated
            # code: that burns attempts and can lock the account.
            if self._2fa_requested_at is None:
                raise AuthRequiredError(
                    "no 2FA request is in flight; POST /login/2fa/request first"
                )
            age = time.monotonic() - self._2fa_requested_at
            if age > self._settings.two_factor_window_s:
                self._2fa_requested_at = None
                raise AuthRequiredError(
                    f"2FA request expired {age - self._settings.two_factor_window_s:.0f}s ago; "
                    "request a new code"
                )
            # Deliberately not refreshed: the code was sent to one of *these*
            # method objects, and an SMS method's phone_number_id must match.
            methods = await self._load_methods()
            index = self._resolve(methods, method_index)
            try:
                state = await methods[index].submit(code)
            finally:
                # Single use either way: a consumed code must not be resubmittable,
                # and a rejected one means asking Apple for a fresh code.
                self._2fa_requested_at = None
                self.save()

            if state == LoginState.LOGGED_IN:
                self.auth_required = False
                self.last_error = None
                self._methods = None
            return str(state)

    async def close(self) -> None:
        self.save()
        if self._account is not None:
            await self._account.close()
