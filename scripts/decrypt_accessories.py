#!/usr/bin/env python
"""Decrypt accessory keys from records collected elsewhere.

Companion to scripts/collect_beacons.sh. That script runs on a Mac where the
BeaconStore key is still readable (macOS <= 14); this one does the decryption,
and can run anywhere FindMy.py runs.

    scripts/decrypt_accessories.py <collected-dir> [--out-dir .devices]

<collected-dir> is what collect_beacons.sh produced: beaconstore.key plus
searchpartyd.tar.gz. A plain path to an unpacked com.apple.icloud.searchpartyd
directory works too, with --key.

Output is one JSON file per accessory, ready for FindMy.py's FindMyAccessory.
Those files contain the master keys: treat them as secrets.
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
import tarfile
import tempfile
from pathlib import Path

from findmy.plist import list_accessories

REPO_ROOT = Path(__file__).resolve().parent.parent


def resolve_inputs(source: Path, key_arg: str | None, workdir: Path) -> tuple[bytes, Path]:
    """Return (key, search_path), unpacking the collected archive if needed."""
    if key_arg:
        key_hex = Path(key_arg).read_text().strip() if Path(key_arg).exists() else key_arg
    else:
        key_file = source / "beaconstore.key"
        if not key_file.exists():
            sys.exit(f"No beaconstore.key in {source}; pass --key explicitly.")
        key_hex = key_file.read_text().strip()

    try:
        key = bytes.fromhex(key_hex)
    except ValueError:
        sys.exit(
            "The BeaconStore key is not hex. On the collecting Mac, "
            "`security find-generic-password -l BeaconStore -w` should print hex; "
            "if it printed an error instead, that Mac cannot supply the key."
        )

    archive = source / "searchpartyd.tar.gz"
    if archive.exists():
        with tarfile.open(archive) as tar:
            tar.extractall(workdir, filter="data")
        search_path = workdir / "com.apple.icloud.searchpartyd"
    elif (source / "OwnedBeacons").is_dir():
        search_path = source
    elif (source / "com.apple.icloud.searchpartyd").is_dir():
        search_path = source / "com.apple.icloud.searchpartyd"
    else:
        sys.exit(f"{source} has neither searchpartyd.tar.gz nor an OwnedBeacons directory.")

    return key, search_path


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", type=Path, help="directory from collect_beacons.sh")
    parser.add_argument("--key", help="BeaconStore key as hex, or a file containing it")
    parser.add_argument("--out-dir", type=Path, default=REPO_ROOT / ".devices")
    args = parser.parse_args()

    workdir = Path(tempfile.mkdtemp(prefix="findmy-decrypt-"))
    try:
        key, search_path = resolve_inputs(args.source, args.key, workdir)

        owned = (
            len(list((search_path / "OwnedBeacons").glob("*")))
            if (search_path / "OwnedBeacons").is_dir()
            else 0
        )
        print(f"Search path: {search_path}  ({owned} owned beacon record(s))")

        accessories = list_accessories(key=key, search_path=search_path)
        print(f"Decrypted {len(accessories)} accessory record(s)\n")

        args.out_dir.mkdir(parents=True, exist_ok=True)
        for accessory in accessories:
            path = args.out_dir / f"{accessory.identifier}.json"
            accessory.to_json(path)
            data = json.loads(path.read_text())
            print(f"  {data.get('name')!r:40} model={data.get('model')!r:22} -> {path.name}")

        print(f"\nWrote {len(accessories)} file(s) to {args.out_dir} -- these contain master keys.")
        print("Next: verify one fetches and decrypts real reports before dismantling anything.")
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


if __name__ == "__main__":
    main()
