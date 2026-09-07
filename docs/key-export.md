# Getting accessory keys out of Apple

Everything in findmy-rest's accessory path depends on one-time per-accessory key
material. Apple does not offer an API for it, so it has to be extracted from a Mac's
keychain or from iCloud Keychain escrow. This is the fiddliest part of the project;
this document records what works, what doesn't, and why.

Keys are **one-time per accessory**. They stay valid indefinitely — an AirTag's rotating
keys are derived from them. Only re-pairing an accessory (factory reset, new owner)
invalidates them and forces a re-export.

## Which route applies

| Where you run it | Local keychain route | iCloud escrow route |
|---|---|---|
| macOS 12–14 | **Works** — `python -m findmy decrypt` | n/a |
| macOS 15 (Sequoia) | Blocked; needs a SIP round trip, and that tool is unmaintained | Should work |
| macOS 26 (Tahoe) | **Dead** — entitlement-gated | Verified working |
| macOS 10.x / 11.x | No accessory records exist at all | n/a |
| Linux, anywhere else | n/a | Should work |

Two things follow from the table:

- **Do not upgrade a macOS 15 machine to 26 while the local route still matters.** The
  upgrade permanently forecloses it.
- The route is chosen by the **macOS version**, not by whose Apple ID owns the
  accessories. A second macOS user account signed into another person's iCloud gets its
  own `~/Library/com.apple.icloud.searchpartyd/` and its own keychain entry, so on a
  macOS 12–14 machine that is a perfectly good way to export someone else's accessories
  — and a *less* invasive one than escrow (see below).

## Route 1: local keychain (macOS 12–14)

The accessory records live in `~/Library/com.apple.icloud.searchpartyd/OwnedBeacons`,
encrypted with a `BeaconStore` key held in the login keychain.

```bash
python -m findmy decrypt --out-dir devices/
```

If the machine doing the decryption isn't the machine holding the keychain — e.g. an old
laptop too old to run Python 3.10+ — split the work. On the old Mac:

```bash
scripts/collect_beacons.sh          # stock tools only: security + tar
```

then bring the resulting directory back and run, on any machine:

```bash
scripts/decrypt_accessories.py <collected-dir>
```

`list_accessories()` takes `key=` and `search_path=`, so the old Mac only ever has to
produce the key and the encrypted records.

Requirements on the source Mac:

- signed into the **owner's** Apple ID, with Find My enabled
- iCloud Keychain enabled (this is how the `BeaconStore` key arrives), which needs
  approval from another of that account's devices or the account security code
- left online long enough to actually sync — `collect_beacons.sh` counts the records
  and refuses if there are none, so you find out before trusting its output

Sharees are out of luck: a shared accessory only leaves `peerTrustSharedSecret` in the
sharee's keychain (FindMy.py issue #34, open since 2024). Export must happen from the
owner's account.

## Route 2: iCloud Keychain escrow (macOS 15 and 26, or no Mac at all)

[stek29/export-findmy](https://github.com/stek29/export-findmy) skips the local keychain
entirely: SRP auth → MobileMe delegate tokens → join the iCloud Keychain trust circle by
escrow recovery → pull BeaconStore records from CloudKit → decrypt → write FindMy.py
JSON. No SIP changes, no second Mac.

Build (this repo's sibling checkout at `../export-findmy`):

```bash
git clone https://github.com/stek29/export-findmy.git
cd export-findmy
git fetch origin pull/1/head:macos-native-anisette && git checkout macos-native-anisette
cargo build --release
```

The PR branch matters on macOS: it uses the Mac's **native AOSKit** anisette instead of a
remote anisette server, so no third party sees the authentication. It is required on
macOS 26, where emulated ADI fails provisioning (`-45054`), and is preferable on 15 for
the privacy reason alone.

Native anisette means the device profile must describe the **real Mac** — an iPhone
profile plus Mac anisette is rejected by Apple's gateway with "We can not process your
request, please try again later." Values for this machine, already written to
`.local/device-profile.toml`:

```toml
[software]
model = "MacBookPro18,2"       # sysctl -n hw.model
model_class = "MacBook Pro"
os_version = "15.6.1"          # sw_vers -productVersion
build = "24G90"                # sw_vers -buildVersion
cfnetwork_version = "3826.600.41"
darwin_version = "24.6.0"      # uname -r
```

Run:

```bash
./target/release/export-findmy \
  --apple-id <owner@example.com> \
  --device-profile .local/device-profile.toml
```

It prompts for: password → 2FA method → 2FA code → **the passcode of one of that
account's real devices** (used for escrow recovery) → an escrow password for the
synthetic exporter record it creates. Output lands in `.local/keys/` as plist + JSON
pairs.

### What this costs, stated plainly

The escrow route **joins the iCloud Keychain trust circle** for the account. That is
broad access to that account's keychain — its saved passwords — not narrow access to
beacon keys. It also registers a synthetic device in the account's escrow.

Consequences worth weighing before pointing it at someone else's Apple ID:

- It needs their **device passcode**, not just their password.
- Route 1 on a macOS 12–14 machine needs neither the passcode nor a trust-circle join,
  so where hardware allows, it is the gentler option for someone else's account.
- Cleaning up the synthetic escrow record — **read this before running it**:
  ```bash
  ./target/release/export-findmy --apple-id <...> \
    --device-profile .local/device-profile.toml --delete-own-escrow-bottle
  ```
  **The model column is not a distinguisher.** Native anisette forces the device
  profile to mirror the real Mac, so the exporter's row and the host Mac's own row show
  the *same* `MacBook Pro, MacBookPro18,2` and the same build. Only the **name** and
  **serial** differ (`findmy-rest exporter` / `F2LZN0FAKE00` versus the machine's real
  name and serial from `ioreg -l | grep IOPlatformSerialNumber`).

  **The credential is the tell, and it fails in a misleading direction.** Each bottle
  unlocks with its own credential: the exporter's with the saved escrow password, a real
  Mac's with that Mac's login password, an iPhone's with its passcode. If pressing Enter
  (saved password) fails with `-6015 CLUBH ERROR: Credential is not verified`, that is
  evidence you picked the **wrong row** — not an invitation to try the device password.
  Trying the device password next is exactly how you delete your own Mac's escrow record.

  Observed on this account (2026-09): the exporter did **not** create a viable bottle at
  all. After a successful export the list contained only real devices. So if no row
  matches `F2LZN0FAKE00`, the correct action is to **press Enter and cancel** — there is
  nothing to clean up.

  If a real device's bottle is deleted by mistake: nothing is signed out and no keychain
  contents are lost; the device simply loses its escrow copy (recovery via that device's
  password). Reboot and re-check first — macOS re-escrows on its own. Failing that,
  toggle System Settings → Apple Account → iCloud → Passwords & Keychain off (keeping the
  local copy) and back on, approving from another device, then confirm the row returns.
- The tool is explicitly experimental. malmeloo declined to document it in FindMy.py yet
  ("still a bit experimental and bound to change") and intends to build the capability
  into FindMy.py directly. Expect to redo this at some point.

## Preferred approach for this project

Own the trackers on **my** Apple ID and share them to the person carrying them:

- the trust circle joined is mine, not an elderly relative's
- no need for her passcode or 2FA at export time, now or on future re-exports
- her iPhone stops raising "item found moving with you" alerts once the items are shared
- re-pairing, battery swaps and re-exports need no phone call

Re-pair **before** exporting — re-pairing invalidates exported keys.

## Naming and deploying the keys

Exported files come out named after Apple's own identifiers — long, and full of
characters that are hostile in a topic or a filename. Rename each one you intend to
deploy to **`<owner>.<device>.json`**, choosing both halves yourself:

```
Jane_s_Keys_2006__0000...EXAMPLE00SER.json    →   jane.keys.json
Kayak_unknown-model_2006__000000...json       →   jane.wallet.json
```

That one decision propagates everywhere: SSM parameter `<base>/jane.keys`, Kubernetes
secret key `jane.keys.json`, and MQTT topic `findmy/jane/keys/...`. Owner ids are yours
to assign, so pick one per person and stay consistent — Apple's names will not (`Jane` in
one place, `Jane Smith` in another, and the AirTags in her bag are named after whoever
paired them, not whoever carries them).

`scripts/sync_ssm.py` pushes a directory of those files into SSM:

```bash
scripts/sync_ssm.py keys/                   # plan only (the default)
scripts/sync_ssm.py keys/ --apply           # write
scripts/sync_ssm.py keys/ --apply --prune   # and delete parameters with no local file
scripts/sync_ssm.py keys/ --external-secret # emit the ExternalSecret data block
```

It refuses to upload anything not matching the convention rather than inventing an
identity, skips parameters whose value is unchanged (SSM keeps only 100 versions), and
prints each file's Apple name beside the slug you chose so you can confirm that
`jane.keys` really is her keys before it goes anywhere.

Configuration comes from the environment, and an `.env` beside the key files is read
automatically (the real environment wins over the file):

```
FINDMY_SSM_BASE=/my-cluster/apps/findmy-rest/accessories
FINDMY_OP_ACCOUNT=my.1password.com
FINDMY_OP_VAULT=MyVault
FINDMY_OP_BACKUP=1
```

With `FINDMY_OP_BACKUP` (or `--1password`), each parameter is also mirrored into
1Password as a Password item titled after the SSM path — uppercased, `/` → `_`, `-` →
`__`, no leading underscore — matching however your other secrets are named:

```
/my-cluster/apps/findmy-rest/accessories/jane.keys
  → MY__CLUSTER_APPS_FINDMY__REST_ACCESSORIES_JANE.KEYS
```

Existing items are edited rather than duplicated. Key material never reaches a command
line — "command arguments can be visible to other users", as `op` puts it — so both
tools are handed a 0600 temp file that is unlinked afterwards. Not stdin, in either
case: AWS CLI v2 cannot read `--cli-input-json file:///dev/stdin`, and `op` treats piped
stdin as a template in its own right and refuses "template and stdin at the same time".

**Deploy only what you actually track.** A full export includes your phones, watches,
Macs and AirPods; putting just the trackers she carries into SSM is least privilege and
less to poll.

## Verify before dismantling

Whatever route produced the keys, confirm they fetch and decrypt a real report before
restoring SIP, returning a borrowed Mac, or wiping intermediates:

```bash
.venv/bin/python examples/airtag.py   # from a FindMy.py checkout, against one exported JSON
```
