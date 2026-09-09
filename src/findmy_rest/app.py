"""The HTTP surface.

One process, one event loop, no worker pool: the Apple session and the anisette
identity have exactly one owner, which is what the PVC-backed state requires.
"""

from __future__ import annotations

import logging
import re
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Query
from pydantic import BaseModel

from . import __version__
from .backends.accessories import AccessoryBackend
from .backends.idevices import FmipBackend
from .config import Settings
from .merge import merge_devices
from .models import Device, Health, Source
from .session import AppleSession, AuthRequiredError

logger = logging.getLogger(__name__)

DURATION = re.compile(r"^(\d+)([smhd])?$")
MULTIPLIER = {"s": 1, "m": 60, "h": 3600, "d": 86400}


class LoginRequest(BaseModel):
    password: str | None = None


class TwoFactorRequest(BaseModel):
    code: str
    # None means "use FINDMY_REST_2FA_PREFER", which is what unattended re-auth
    # relies on; an explicit index overrides it.
    method_index: int | None = None


def parse_duration(value: str | None) -> float | None:
    """Accept `900`, `15m`, `2h`, `1d`."""
    if not value:
        return None
    match = DURATION.match(value.strip())
    if not match:
        raise HTTPException(400, f"invalid duration: {value!r} (try 30m, 2h, 900)")
    amount, unit = match.groups()
    return int(amount) * MULTIPLIER.get(unit or "s", 1)


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or Settings.from_env()
    session = AppleSession(settings)
    accessories = AccessoryBackend(settings, session)
    fmip = FmipBackend(settings)

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        # Startup: restore state from the PVC. A missing or dead session is not an
        # error here -- we come up, report auth_required, and wait for /login.
        await session.load()
        accessories.load_keys()
        yield
        await accessories.aclose()
        await session.close()

    app = FastAPI(
        title="findmy-rest",
        description="REST API for Apple Find My accessory and device location and battery",
        version=__version__,
        lifespan=lifespan,
    )

    async def collect(source: Source | None, *, force: bool, wait: bool = False) -> list[Device]:
        devices: list[Device] = []
        if source in (None, Source.FINDMY):
            devices += await accessories.fetch(force=force, wait=wait)
        if source in (None, Source.FMIP):
            devices += await fmip.fetch(force=force)
        # Accessories first, so a tie goes to the backend that works without
        # iCloud. Merging runs even for a single backend: it is cheap, and it
        # keeps one code path rather than two.
        return merge_devices(devices)

    @app.get("/devices")
    async def get_devices(
        source: Source | None = None,
        located: bool = Query(default=False, description="drop devices with no location"),
        max_age: str | None = Query(default=None, description="drop fixes older than e.g. 30m"),
        force: bool = Query(default=False, description="bypass the minimum fetch interval"),
        wait: bool = Query(
            default=False, description="block for the refresh instead of returning cached"
        ),
    ):
        devices = await collect(source, force=force, wait=wait)

        if located:
            devices = [d for d in devices if d.has_location]

        cutoff = parse_duration(max_age)
        if cutoff is not None:
            devices = [
                d for d in devices if d.location_age_s is not None and d.location_age_s <= cutoff
            ]

        return [d.serialize() for d in devices]

    @app.get("/health")
    async def health() -> Health:
        # Healthy means "serving": a backend needing auth is reported, not fatal,
        # so Kubernetes does not restart us into a 2FA loop.
        return Health(
            ok=True,
            findmy=accessories.health,
            fmip=fmip.health,
            two_factor_pending=session.two_factor_pending,
        )

    @app.post("/login")
    async def login(body: LoginRequest | None = None):
        try:
            return await session.login(body.password if body else None)
        except AuthRequiredError as exc:
            raise HTTPException(400, str(exc)) from exc

    @app.post("/login/2fa/request")
    async def request_2fa(method_index: int | None = None):
        try:
            chosen = await session.request_2fa(method_index)
        except AuthRequiredError as exc:
            raise HTTPException(400, str(exc)) from exc
        return {"requested": True, "method_index": chosen}

    @app.post("/login/2fa")
    async def submit_2fa(body: TwoFactorRequest):
        try:
            state = await session.submit_2fa(body.code, body.method_index)
        except AuthRequiredError as exc:
            raise HTTPException(400, str(exc)) from exc
        if not session.auth_required:
            accessories.load_keys()
        return {"state": state, "auth_required": session.auth_required}

    return app
