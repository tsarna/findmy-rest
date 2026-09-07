#!/usr/bin/env python
"""Prove exported accessory keys fetch and decrypt real location reports.

This is the last unproven link before the service is worth writing, and it
deliberately mirrors what the service will do at runtime:

  * local anisette (Unicorn), with the libs bundle cached on disk
  * account state persisted to JSON and restored on the next run, so password
    and 2FA are needed once rather than every poll
  * one batched fetch covering every accessory

    scripts/verify_reports.py                 # all exported accessories
    scripts/verify_reports.py --history       # every report, not just the newest
    scripts/verify_reports.py --name Keys     # only accessories matching a substring

State lives in .state/ (gitignored): account.json, anisette_libs.bin.
Both are secrets -- account.json is a logged-in Apple session.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import shutil
import sys
from datetime import datetime, timezone
from getpass import getpass
from pathlib import Path

from findmy import FindMyAccessory
from findmy.reports import AsyncAppleAccount, LocalAnisetteProvider, LoginState

from findmy_rest.alignment import AlignmentStore

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_STATE_DIR = REPO_ROOT / ".state"
DEFAULT_KEYS_DIR = Path.home() / "findmy"


class StatePaths:
    """Where one account's session and anisette identity live.

    Per-account by design: an anisette identity is a virtual device and the
    session is bound to one login, so sharing a directory between two Apple IDs
    means whichever logged in last wins.
    """

    def __init__(self, state_dir: Path) -> None:
        self.dir = state_dir
        self.account = state_dir / "account.json"
        self.anisette = state_dir / "anisette.json"
        self.libs = state_dir / "anisette_libs.bin"

    def libs_path(self) -> str | None:
        return str(self.libs) if self.libs.exists() else None


def load_accessories(keys_dir: Path, name_filter: str | None) -> dict[str, FindMyAccessory]:
    """Map filename stem -> accessory.

    Keyed by stem, not by Apple name, to match how the service keys its alignment
    cache: state produced here can then be copied straight onto the service's
    volume and actually be used, instead of sitting there under a key nothing
    looks up.
    """
    files = sorted(keys_dir.glob("*.json"))
    if not files:
        sys.exit(f"No accessory JSON files in {keys_dir}")

    accessories: dict[str, FindMyAccessory] = {}
    for path in files:
        try:
            accessory = FindMyAccessory.from_json(path)
        except Exception as exc:  # noqa: BLE001 - diagnostic script
            print(f"  ! could not load {path.name}: {exc}")
            continue
        if name_filter and name_filter.lower() not in (accessory.name or "").lower():
            continue
        accessories[path.name.removesuffix(".json")] = accessory

    if not accessories:
        sys.exit(f"No accessories matched {name_filter!r}")
    return accessories


async def do_2fa(account: AsyncAppleAccount) -> None:
    methods = await account.get_2fa_methods()
    if not methods:
        sys.exit("2FA required but no methods offered.")

    print("\n2FA required. Available methods:")
    for i, method in enumerate(methods):
        phone = getattr(method, "phone_number", None)
        print(f"  [{i}] {type(method).__name__}" + (f" -> {phone}" if phone else ""))

    choice = input(f"Method [0-{len(methods) - 1}], default 0: ").strip() or "0"
    method = methods[int(choice)]

    await method.request()
    code = input("Code: ").strip()
    state = await method.submit(code)
    if state != LoginState.LOGGED_IN:
        sys.exit(f"2FA did not complete; login state is {state}")


def build_anisette(paths: StatePaths) -> LocalAnisetteProvider:
    """Reuse one virtual device across runs.

    Without this, every attempt provisions a *new* anisette identity, so a
    handful of retries look to Apple like logins from a handful of new devices --
    a good way to attract a 503 from GSA. The provisioning state is cheap (7 KB)
    and the libs bundle is 2.2 MB, so both are worth keeping.
    """
    if paths.anisette.exists():
        print(f"Reusing anisette identity from {paths.anisette}")
        return LocalAnisetteProvider.from_json(paths.anisette, libs_path=paths.libs_path())
    print("No saved anisette identity; provisioning a new virtual device.")
    return LocalAnisetteProvider(libs_path=paths.libs_path())


async def get_account(apple_id: str, paths: StatePaths) -> AsyncAppleAccount:
    """Restore a saved session, or log in and save one."""
    paths.dir.mkdir(parents=True, exist_ok=True)

    if paths.account.exists():
        account = AsyncAppleAccount.from_json(paths.account, anisette_libs_path=paths.libs_path())
        print(f"Restored session from {paths.account} (state: {account.login_state})")
        if account.login_state == LoginState.LOGGED_IN:
            return account
        print("Saved session is no longer logged in; authenticating again.")
        anisette = account.anisette if hasattr(account, "anisette") else build_anisette(paths)
    else:
        anisette = build_anisette(paths)
        account = AsyncAppleAccount(anisette)

    # Persist the anisette identity before touching Apple, so a failed or abandoned
    # login attempt still leaves the same virtual device in place for the retry.
    try:
        anisette.to_json(paths.anisette)
    except Exception as exc:  # noqa: BLE001 - best effort
        print(f"(could not save anisette state: {exc})")

    password = getpass(f"iCloud password for {apple_id}: ")
    try:
        state = await account.login(apple_id, password)
        if state == LoginState.REQUIRE_2FA:
            await do_2fa(account)
        elif state != LoginState.LOGGED_IN:
            sys.exit(f"Unexpected login state: {state}")
    finally:
        try:
            anisette.to_json(paths.anisette)
        except Exception:  # noqa: BLE001, S110 - best effort on the error path
            pass

    return account


def describe(report) -> str:
    when = report.timestamp
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    age_min = (datetime.now(tz=timezone.utc) - when).total_seconds() / 60
    return (
        f"{when.isoformat(timespec='seconds')} ({age_min:6.0f} min ago)  "
        f"lat={report.latitude:.6f} lon={report.longitude:.6f}  "
        f"±{report.horizontal_accuracy}m  status={report.status} conf={report.confidence}"
    )


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--apple-id",
        default=os.environ.get("FINDMY_REST_APPLE_ID", ""),
        help="account to log in as (env: FINDMY_REST_APPLE_ID)",
    )
    parser.add_argument(
        "--state-dir",
        type=Path,
        default=DEFAULT_STATE_DIR,
        help="session + anisette identity; keep one per Apple ID (default: .state)",
    )
    parser.add_argument("--keys-dir", type=Path, default=DEFAULT_KEYS_DIR)
    parser.add_argument("--name", help="only accessories whose name contains this")
    parser.add_argument("--history", action="store_true", help="all reports, not just the newest")
    args = parser.parse_args()
    if not args.apple_id:
        sys.exit("No Apple ID: pass --apple-id or set FINDMY_REST_APPLE_ID")

    paths = StatePaths(args.state_dir)
    paths.dir.mkdir(parents=True, exist_ok=True)

    # The libs bundle is account-independent, so reuse a cached one rather than
    # re-downloading 2.2 MB for each new state directory.
    shared_libs = DEFAULT_STATE_DIR / "anisette_libs.bin"
    if not paths.libs.exists() and shared_libs.exists() and shared_libs != paths.libs:
        shutil.copy2(shared_libs, paths.libs)
        print(f"Reused cached anisette libs from {shared_libs}")

    print(f"Apple ID:  {args.apple_id}")
    print(f"State dir: {paths.dir}")
    accessories = load_accessories(args.keys_dir, args.name)
    print(f"Loaded {len(accessories)} accessory/accessories from {args.keys_dir}")

    # Key derivation ratchets forward from the last known index, so a stale
    # accessory costs minutes of CPU before a single request goes out.
    alignment = AlignmentStore(paths.dir / "alignment.json")
    for stem, accessory in accessories.items():
        alignment.apply(stem, accessory)

    account = await get_account(args.apple_id, paths)
    try:
        # Persist immediately: a fresh session is the expensive thing here, and we do
        # not want a fetch error to cost the 2FA round trip.
        account.to_json(paths.account)
        if not paths.libs.exists():
            print(f"(anisette libs will be cached at {paths.libs} by the account state)")
        print(f"Saved session to {paths.account}\n")

        print(f"Fetching reports for {len(accessories)} accessory/accessories in one call...")
        wanted = list(accessories.values())
        if args.history:
            results = await account.fetch_location_history(wanted)
        else:
            results = await account.fetch_location(wanted)

        found = 0
        for stem, accessory in accessories.items():
            reports = results.get(accessory) if isinstance(results, dict) else results
            label = f"{stem}  {accessory.name!r} ({accessory.model or 'unknown model'})"
            if not reports:
                print(f"\n  {label}: no reports")
                continue

            if not isinstance(reports, list):
                reports = [reports]
            reports = [r for r in reports if r is not None]
            found += len(reports)
            print(f"\n  {label}: {len(reports)} report(s)")
            for report in sorted(reports, key=lambda r: r.timestamp, reverse=True)[:10]:
                print(f"      {describe(report)}")

        for stem, accessory in accessories.items():
            alignment.record(stem, accessory)
        alignment.save()

        print(f"\n{found} decrypted report(s) total.")
        if found:
            print("Keys are valid and the fetch path works end to end.")
        else:
            print(
                "No reports came back. That is not necessarily a key problem: an accessory "
                "only reports when a passing Apple device has seen it recently. Try "
                "--history, or move the tracker near an iPhone and retry in ~10 minutes."
            )
    finally:
        account.to_json(paths.account)
        await account.close()


if __name__ == "__main__":
    asyncio.run(main())
