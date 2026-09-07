#!/usr/bin/env python
"""Sync a directory of accessory keys into SSM Parameter Store.

The filename *is* the identity. A key named `jane.keys.json` becomes the parameter
`<base>/jane.keys`, the secret key `jane.keys.json`, and the MQTT topic segment pair
`jane/keys` -- one name chosen once, agreeing everywhere by construction. So this
script refuses to upload anything not named `<owner>.<device>.json` with valid
slugs, rather than quietly inventing an identity.

    scripts/sync_ssm.py keys/                      # plan only (default)
    scripts/sync_ssm.py keys/ --apply              # write
    scripts/sync_ssm.py keys/ --apply --prune      # also delete what is not local
    scripts/sync_ssm.py keys/ --external-secret    # emit the ExternalSecret block

Optionally mirrors each parameter into 1Password, titled after the SSM path the
same way your other secrets are named. Settings come from the environment, and an
`.env` beside the key files is read automatically:

    FINDMY_SSM_BASE=/my-cluster/apps/findmy-rest/accessories
    FINDMY_OP_ACCOUNT=my.1password.com
    FINDMY_OP_VAULT=MyVault
    FINDMY_OP_BACKUP=1

Key material is never printed and never passed on a command line ("Command
arguments can be visible to other users", as `op` puts it): both tools are handed
a 0600 temp file, unlinked afterwards. Not stdin, in either case -- see the
comments on `aws()` and `op()` for why neither accepts it.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from findmy_rest.identity import valid_slug

DEFAULT_BASE = "/findmy-rest/accessories"


def load_env(keys_dir: Path, explicit: Path | None) -> None:
    """Read an .env beside the key files into os.environ.

    The real environment wins: a file is a default, not an override.
    """
    path = explicit or (keys_dir / ".env")
    if not path.exists():
        return
    for raw in path.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip().strip("\"'"))


def op_title(ssm_path: str) -> str:
    """SSM path -> 1Password item title.

    Uppercased, `-` becomes `__` and `/` becomes `_`, with no leading underscore:
    `/my-cluster/apps/findmy-rest/accessories/jane.keys`
        -> `MY__CLUSTER_APPS_FINDMY__REST_ACCESSORIES_JANE.KEYS`

    Single pass, so the two substitutions cannot feed each other.
    """
    out = []
    for char in ssm_path.upper():
        out.append("__" if char == "-" else "_" if char == "/" else char)
    return "".join(out).lstrip("_")


def op(
    *args: str, template_json: str | None = None, account: str | None = None
) -> subprocess.CompletedProcess:
    """Run an op command.

    A template goes in a 0600 temp file, not on stdin: `op` treats piped stdin as
    a template in its own right and refuses "template and stdin at the same
    time". A file also keeps the value out of argv, which `op` itself warns about.
    """
    tmp_path = None
    try:
        cmd = ["op", *args]
        if template_json is not None:
            fd, tmp_path = tempfile.mkstemp(prefix="findmy-op-", suffix=".json")
            os.close(fd)
            Path(tmp_path).chmod(0o600)
            Path(tmp_path).write_text(template_json)
            cmd += ["--template", tmp_path]
        if account:
            cmd += ["--account", account]
        return subprocess.run(cmd, capture_output=True, text=True, check=False)
    finally:
        if tmp_path:
            Path(tmp_path).unlink(missing_ok=True)


def op_backup(title: str, value: str, *, vault: str, account: str | None) -> str:
    """Create or update the 1Password item. Returns what was done."""
    # Title and vault go as flags; the template carries only the secret itself.
    template = json.dumps(
        {
            "category": "PASSWORD",
            "fields": [
                {"id": "password", "type": "CONCEALED", "purpose": "PASSWORD", "value": value}
            ],
        }
    )

    exists = op("item", "get", title, "--vault", vault, "--format", "json", account=account)
    if exists.returncode == 0:
        verb = "edit"
    elif any(
        marker in exists.stderr.lower()
        for marker in ("isn't an item", "not found", "no item matches", "doesn't exist")
    ):
        verb = "create"
    else:
        # Anything else -- not signed in, no network -- must not be read as
        # "absent", or a blip turns an update into a duplicate item.
        sys.exit(f"op item get failed for {title}:\n{exists.stderr.strip()}")
    args = ["item", verb]
    if verb == "edit":
        args.append(title)
    else:
        args += ["--title", title]
    args += ["--vault", vault]

    result = op(*args, template_json=template, account=account)
    if result.returncode != 0:
        sys.exit(f"op item {verb} failed for {title}:\n{result.stderr.strip()}")
    return f"{verb}d"


def aws(*args: str, input_json: str | None = None) -> dict:
    """Run an aws CLI command, returning parsed JSON (or {} for empty output).

    `input_json` is passed via a 0600 temp file rather than argv, so secrets stay
    out of `ps` and shell history. Not over stdin: AWS CLI v2 cannot read
    `--cli-input-json file:///dev/stdin` and fails with "Invalid JSON received".
    """
    tmp_path = None
    cmd = ["aws", *args]
    try:
        if input_json is not None:
            fd, tmp_path = tempfile.mkstemp(prefix="findmy-ssm-", suffix=".json")
            os.close(fd)
            Path(tmp_path).chmod(0o600)
            Path(tmp_path).write_text(input_json)
            cmd += ["--cli-input-json", f"file://{tmp_path}"]
        cmd += ["--output", "json"]

        proc = subprocess.run(cmd, capture_output=True, text=True, check=False)
        if proc.returncode != 0:
            sys.exit(f"aws {' '.join(args[:2])} failed:\n{proc.stderr.strip()}")
        return json.loads(proc.stdout) if proc.stdout.strip() else {}
    finally:
        if tmp_path:
            Path(tmp_path).unlink(missing_ok=True)


def parse_filename(path: Path) -> tuple[str, str]:
    """Enforce `<owner>.<device>.json`, or explain exactly what is wrong."""
    stem = path.name.removesuffix(".json")
    owner, sep, device = stem.partition(".")
    if not sep:
        sys.exit(
            f"{path.name}: expected '<owner>.<device>.json' (e.g. jane.keys.json).\n"
            f"  The owner id is yours to assign -- pick one and use it consistently."
        )
    if "." in device:
        sys.exit(f"{path.name}: too many dots; the device half may not contain one.")
    for part, label in ((owner, "owner"), (device, "device")):
        if not valid_slug(part):
            sys.exit(
                f"{path.name}: {label} {part!r} is not a valid slug.\n"
                f"  Use lowercase letters, digits and dashes, starting alphanumeric."
            )
    return owner, device


def local_keys(keys_dir: Path) -> dict[str, tuple[Path, dict]]:
    """Map `<owner>.<device>` -> (path, parsed json)."""
    if not keys_dir.is_dir():
        sys.exit(f"{keys_dir} is not a directory")

    found: dict[str, tuple[Path, dict]] = {}
    for path in sorted(keys_dir.glob("*.json")):
        owner, device = parse_filename(path)
        try:
            data = json.loads(path.read_text())
        except ValueError as exc:
            sys.exit(f"{path.name}: not valid JSON ({exc})")
        if "master_key" not in data:
            sys.exit(f"{path.name}: no master_key field; is this an exported accessory?")
        found[f"{owner}.{device}"] = (path, data)

    if not found:
        sys.exit(f"No *.json accessory files in {keys_dir}")
    return found


def remote_keys(base: str) -> dict[str, str]:
    """Map parameter leaf name -> current value."""
    result = aws(
        "ssm",
        "get-parameters-by-path",
        "--path",
        base,
        "--with-decryption",
        "--recursive",
    )
    return {p["Name"].rsplit("/", 1)[-1]: p["Value"] for p in result.get("Parameters", [])}


def put(base: str, leaf: str, value: str, *, key_id: str | None, overwrite: bool) -> None:
    payload = {
        "Name": f"{base}/{leaf}",
        "Type": "SecureString",
        "Value": value,
        "Overwrite": overwrite,
        "Description": "findmy-rest accessory key (managed by scripts/sync_ssm.py)",
    }
    if key_id:
        payload["KeyId"] = key_id
    # Via a temp file, not argv: the value is a private key.
    aws("ssm", "put-parameter", input_json=json.dumps(payload))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("keys_dir", type=Path, help="directory of <owner>.<device>.json files")
    parser.add_argument("--base-path", help="SSM prefix (env: FINDMY_SSM_BASE)")
    parser.add_argument("--env-file", type=Path, help="default: <keys_dir>/.env")
    parser.add_argument(
        "--1password",
        dest="onepassword",
        action=argparse.BooleanOptionalAction,
        default=None,
        help="also mirror into 1Password (env: FINDMY_OP_BACKUP)",
    )
    parser.add_argument("--op-vault", help="env: FINDMY_OP_VAULT")
    parser.add_argument("--op-account", help="env: FINDMY_OP_ACCOUNT")
    parser.add_argument("--apply", action="store_true", help="actually write (default: plan only)")
    parser.add_argument("--prune", action="store_true", help="delete parameters with no local file")
    parser.add_argument("--key-id", help="KMS key for SecureString (default: alias/aws/ssm)")
    parser.add_argument(
        "--external-secret", action="store_true", help="print the ExternalSecret data block"
    )
    args = parser.parse_args()
    load_env(args.keys_dir, args.env_file)

    # Precedence: flag > environment (including .env) > default.
    base_path = args.base_path or os.environ.get("FINDMY_SSM_BASE") or DEFAULT_BASE
    op_vault = args.op_vault or os.environ.get("FINDMY_OP_VAULT")
    op_account = args.op_account or os.environ.get("FINDMY_OP_ACCOUNT")
    if args.onepassword is None:
        backup = os.environ.get("FINDMY_OP_BACKUP", "").strip().lower() in {
            "1",
            "true",
            "yes",
            "on",
        }
    else:
        backup = args.onepassword
    if backup and not op_vault:
        sys.exit("1Password backup needs a vault: --op-vault or FINDMY_OP_VAULT")

    local = local_keys(args.keys_dir)
    remote = remote_keys(base_path)

    creates, updates, unchanged = [], [], []
    for leaf, (path, data) in sorted(local.items()):
        if leaf not in remote:
            creates.append((leaf, path, data))
        elif json.loads(remote[leaf]) != data:
            updates.append((leaf, path, data))
        else:
            unchanged.append((leaf, path, data))

    orphans = sorted(set(remote) - set(local))

    print(f"Base path: {base_path}")
    print(f"Local:     {args.keys_dir} ({len(local)} key file(s))")
    if backup:
        print(f"1Password: vault {op_vault}" + (f" on {op_account}" if op_account else ""))
    print()

    header = f"{'slug':22} {'action':10} Apple name"
    print(header)
    print("-" * len(header))
    for action, group in (("create", creates), ("update", updates), ("unchanged", unchanged)):
        for leaf, _path, data in group:
            owner, _, device = leaf.partition(".")
            print(f"{owner + '/' + device:22} {action:10} {data.get('name', '?')!r}")
    for leaf in orphans:
        print(f"{leaf.replace('.', '/'):22} {'orphan':10} (in SSM, no local file)")

    if args.external_secret:
        print("\n# ExternalSecret data block:")
        for leaf in sorted(local):
            print(f"  - secretKey: {leaf}.json")
            print(f"    remoteRef:\n      key: {base_path}/{leaf}")

    if not args.apply:
        todo = len(creates) + len(updates) + (len(orphans) if args.prune else 0)
        if backup and (creates or updates):
            print("\n# 1Password items that would be written:")
            for leaf, _path, _data in creates + updates:
                print(f"  {op_title(f'{base_path}/{leaf}')}")
        print(f"\nPlan only. {todo} change(s) would be made. Re-run with --apply.")
        return

    for leaf, _path, data in creates + updates:
        value = json.dumps(data)
        put(base_path, leaf, value, key_id=args.key_id, overwrite=leaf in remote)
        print(f"  wrote {base_path}/{leaf}")
        if backup:
            title = op_title(f"{base_path}/{leaf}")
            action = op_backup(title, value, vault=op_vault, account=op_account)
            print(f"    1Password: {action} {title}")

    if args.prune:
        for leaf in orphans:
            aws("ssm", "delete-parameter", "--name", f"{base_path}/{leaf}")
            print(f"  deleted {base_path}/{leaf}")
    elif orphans:
        print(f"\n{len(orphans)} orphan(s) left in place; --prune removes them.")

    print(
        f"\nDone: {len(creates)} created, {len(updates)} updated, "
        f"{len(unchanged)} unchanged" + (f", {len(orphans)} deleted" if args.prune else "")
    )


if __name__ == "__main__":
    main()
