"""What a backend does after a failed fetch: classify it, then wait before retrying.

Both rules exist because of what happened when neither did. From 2026-09-14 the
iCloud backend failed every poll (the account's terms of service had changed) and,
since a failure never counted as a fetch, retried a full Apple login on *every*
request: ~287 failed logins a day for five weeks. And because "needs re-auth" was
decided by searching the message for "2fa" or "login", none of those failures --
"You must accept the updated terms of service", "Re-authentication required" --
ever set `auth_required`, so nothing alerted.

So: a failure always counts as a fetch for the floor, repeated failures back off
exponentially, and anything that only a human can fix backs off straight to the cap.
Retrying cannot clear an auth failure, and every retry is one more failed login on
Apple's books for this account.
"""

from __future__ import annotations

import time

# Matched by class name, through the MRO, so neither optional library has to be
# importable here (pyicloud is only installed with the fmip extra).
AUTH_FAILURES = frozenset(
    {
        # findmy-rest's own: no logged-in Apple session.
        "AuthRequiredError",
        # FindMy.py. UnhandledProtocolError is how GSA refusals surface (the
        # 2026-09 "Error response for GSA request: 503"); UnauthorizedError is a
        # report fetch answering 401, e.g. a session with no searchPartyToken.
        "InvalidCredentialsError",
        "UnauthorizedError",
        "UnhandledProtocolError",
        # pyicloud.
        "PyiCloudFailedLoginException",
        "PyiCloudAcceptTermsException",
        "PyiCloudAuthRequiredException",
        "PyiCloud2FARequiredException",
        "PyiCloud2SARequiredException",
        "PyiCloudPasswordException",
        "PyiCloudNoStoredPasswordAvailableException",
        "PyiCloudNoTrustedNumberAvailable",
        "PyiCloudTrustedDevicePromptException",
        "PyiCloudTrustedDeviceVerificationException",
    }
)

# Long enough that a dead session costs Apple four failed logins a day, short
# enough that a fix made outside this service (accepting new terms on the web) is
# picked up the same day without a restart.
MAX_BACKOFF_S = 6 * 3600


def is_auth_failure(exc: BaseException) -> bool:
    """True when retrying cannot help: Apple wants a human, not another attempt."""
    return any(cls.__name__ in AUTH_FAILURES for cls in type(exc).__mro__)


class Backoff:
    """When the next attempt is allowed, after zero or more consecutive failures."""

    def __init__(self, *, cap_s: float = MAX_BACKOFF_S, clock=time.monotonic) -> None:
        self._cap_s = cap_s
        self._clock = clock
        self.failures = 0
        self._not_before = 0.0

    def ready(self) -> bool:
        return self._clock() >= self._not_before

    def retry_in_s(self) -> float | None:
        """Seconds until the next attempt is allowed, or None if not backing off."""
        if not self.failures:
            return None
        return max(0.0, self._not_before - self._clock())

    def failed(self, floor_s: float, *, auth: bool) -> float:
        """Record a failure; return the delay before the next attempt.

        The first plain failure waits one floor, as a success would. Each further
        one doubles it, up to the cap. An auth failure goes straight to the cap.
        """
        self.failures += 1
        delay = self._cap_s if auth else floor_s * 2 ** (self.failures - 1)
        delay = min(max(delay, floor_s), self._cap_s)
        self._not_before = self._clock() + delay
        return delay

    def succeeded(self) -> None:
        self.failures = 0
        self._not_before = 0.0
