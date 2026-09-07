#!/usr/bin/env python
"""Step 1a discovery: what does the iCloud FMIP 'devices' endpoint actually return?

Answers the open question in tasks/todo.md: which family members' phones and laptops
are visible to one account, and what battery/location data comes back for each.

Usage:
    scripts/probe_devices.py [apple_id]

    APPLE_ID / ICLOUD_PASSWORD may be set in the environment instead; the password
    is prompted for if not set. The session is cached in .icloud-session/ so repeat
    runs skip the 2FA prompt -- the same trick the service will use on its PVC.
"""

from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timezone
from getpass import getpass
from pathlib import Path
from typing import Any

from pyicloud import PyiCloudService

REPO_ROOT = Path(__file__).resolve().parent.parent
SESSION_DIR = REPO_ROOT / ".icloud-session"
RAW_DUMP = REPO_ROOT / "probe-devices.json"


def login(apple_id: str) -> PyiCloudService:
    password = os.environ.get("ICLOUD_PASSWORD") or getpass(f"iCloud password for {apple_id}: ")
    SESSION_DIR.mkdir(exist_ok=True)

    api = PyiCloudService(
        apple_id,
        password,
        cookie_directory=str(SESSION_DIR),
        with_family=True,
    )

    if api.requires_2fa:
        print(f"\n2FA required. Delivery method: {api.two_factor_delivery_method}")
        if api.two_factor_delivery_notice:
            print(f"Notice: {api.two_factor_delivery_notice}")
        code = input("Enter the code you received: ").strip()
        if not api.validate_2fa_code(code):
            sys.exit("2FA code rejected.")
        if not api.is_trusted_session:
            print("Requesting session trust (so the next run skips 2FA)...")
            api.trust_session()
    elif api.requires_2sa:
        # Legacy two-step accounts take a different path; handle if we ever hit it.
        sys.exit("Account uses legacy 2SA, not 2FA -- unhandled in this probe.")

    print(f"Trusted session: {api.is_trusted_session}")
    return api


def age(timestamp_ms: Any) -> str:
    if not isinstance(timestamp_ms, (int, float)):
        return "-"
    when = datetime.fromtimestamp(timestamp_ms / 1000, tz=timezone.utc)
    return f"{(datetime.now(tz=timezone.utc) - when).total_seconds() / 60:.0f}m ago"


def schema_summary(raw: list[dict[str, Any]]) -> None:
    """Field shapes and filter-candidate distributions, with no identifying values.

    Safe to paste anywhere: prints key names, types, and the distribution of low-
    cardinality status fields -- never names, coordinates, serials or identifiers.
    """
    identifying = {"prsId", "ownerNumber", "baUUID", "id", "deviceDiscoveryId"}
    filter_candidates = (
        "deviceStatus",
        "batteryStatus",
        "isMac",
        "fmlyShare",
        "locationEnabled",
        "locationCapable",
        "lostModeCapable",
        "isConsideredAccessory",
        "activationLocked",
        "deviceClass",
        "modelDisplayName",
    )

    print("\n=== field presence (count/total, types) ===")
    keys: dict[str, int] = {}
    types: dict[str, set[str]] = {}
    for device in raw:
        for k, v in device.items():
            keys[k] = keys.get(k, 0) + 1
            types.setdefault(k, set()).add(type(v).__name__)
    for k in sorted(keys):
        print(f"  {k:28} {keys[k]:>3}/{len(raw)}  {sorted(types[k])}")

    print("\n=== filter-candidate value distributions ===")
    for field in filter_candidates:
        counts: dict[str, int] = {}
        for device in raw:
            key = repr(device.get(field))
            counts[key] = counts.get(key, 0) + 1
        if counts and list(counts) != ["None"]:
            print(f"  {field:24} {counts}")

    print("\n=== location ===")
    with_loc = sum(1 for d in raw if d.get("location"))
    print(f"  {with_loc}/{len(raw)} have a location object")
    loc_keys: dict[str, int] = {}
    for device in raw:
        for k in device.get("location") or {}:
            loc_keys[k] = loc_keys.get(k, 0) + 1
    print(f"  location subfields: {loc_keys}")
    print(f"\n  (identifying fields deliberately not summarized: {sorted(identifying)})")


def main() -> None:
    apple_id = (sys.argv[1] if len(sys.argv) > 1 else os.environ.get("APPLE_ID", "")).strip()
    if not apple_id:
        sys.exit("Usage: scripts/probe_devices.py <apple_id>  (or set APPLE_ID)")

    api = login(apple_id)

    manager = api.devices
    manager.refresh()  # family devices arrive on a follow-up poll, not the first response

    devices = list(manager)
    print(f"\n{len(devices)} device(s) returned\n")

    # Dump raw first: everything below is best-effort formatting, and we do not want a
    # display bug to cost us the response we just spent a 2FA round trip on.
    try:
        user_info = manager.user_info
    except Exception as exc:  # noqa: BLE001 - diagnostic script
        user_info = {"error": repr(exc)}
    raw: list[dict[str, Any]] = [d.data for d in devices]
    RAW_DUMP.write_text(json.dumps({"userInfo": user_info, "devices": raw}, indent=2))
    print(f"Raw response written to {RAW_DUMP}\n")

    header = f"{'name':28} {'model':22} {'batt':>6} {'status':10} {'located':>10}  owner-ish fields"
    print(header)
    print("-" * len(header))

    for device in devices:
        data = device.data
        location = data.get("location") or {}
        level = data.get("batteryLevel")
        battery = f"{level * 100:.0f}%" if isinstance(level, (int, float)) and level else "-"

        # Fields that might distinguish a family member's device from one of ours.
        owner_hints = {
            k: v
            for k, v in data.items()
            if k
            in (
                "isMac",
                "fmlyShare",
                "prsId",
                "ownerNumber",
                "rawDeviceModel",
                "deviceDiscoveryId",
                "baUUID",
            )
            and v not in (None, "", False)
        }

        print(
            f"{str(data.get('name'))[:28]:28} "
            f"{str(data.get('deviceDisplayName'))[:22]:22} "
            f"{battery:>6} "
            f"{str(data.get('batteryStatus'))[:10]:10} "
            f"{age(location.get('timeStamp')):>10}  "
            f"{owner_hints}"
        )

        if location:
            print(
                f"    lat={location.get('latitude')} lon={location.get('longitude')} "
                f"horizontalAccuracy={location.get('horizontalAccuracy')} "
                f"positionType={location.get('positionType')} "
                f"isOld={location.get('isOld')} isInaccurate={location.get('isInaccurate')}"
            )
        else:
            print("    (no location)")

    schema_summary(raw)


if __name__ == "__main__":
    main()
