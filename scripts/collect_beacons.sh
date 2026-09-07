#!/bin/bash
# Run this ON THE OLD MAC (macOS 14 or earlier, signed into the accessory owner's
# Apple ID). It needs nothing but stock macOS tools -- no Python, no FindMy.py.
#
# It collects the two things needed to decrypt accessory keys elsewhere:
#   1. the BeaconStore key from the login keychain (gone from `security` on macOS 15)
#   2. the encrypted searchpartyd records
#
# Copy the resulting archive back to the machine running findmy-rest and feed it to
# scripts/decrypt_accessories.py.
#
# BOTH OUTPUTS ARE SECRETS. Transfer them over something private and delete the
# archive from both machines once the device JSON is verified.

set -euo pipefail

SP_DIR="$HOME/Library/com.apple.icloud.searchpartyd"
OUT="${1:-$HOME/Desktop/beacons-$(date +%Y%m%d-%H%M%S)}"

echo "macOS version: $(sw_vers -productVersion)"

case "$(sw_vers -productVersion)" in
    15.*|16.*|17.*|18.*|19.*|2*)
        echo
        echo "WARNING: this looks like macOS 15 or later, where the BeaconStore key has"
        echo "moved to the data-protection keychain and 'security' cannot read it."
        echo "This script will probably fail. That is the case that needs"
        echo "pajowu/beaconstorekey-extractor and the SIP round trip instead."
        echo
        ;;
esac

if [ ! -d "$SP_DIR/OwnedBeacons" ]; then
    echo "ERROR: $SP_DIR/OwnedBeacons does not exist." >&2
    echo "Is this Mac signed into iCloud with Find My enabled?" >&2
    exit 1
fi

owned=$(ls "$SP_DIR/OwnedBeacons" 2>/dev/null | wc -l | tr -d ' ')
shared=$(ls "$SP_DIR/SharedBeacons" 2>/dev/null | wc -l | tr -d ' ')
echo "OwnedBeacons:  $owned"
echo "SharedBeacons: $shared  (these are shared TO this account and lack usable keys)"

if [ "$owned" -eq 0 ]; then
    echo >&2
    echo "ERROR: no owned beacons found. This Mac has not synced the accessory records." >&2
    echo "Sign into the owner's Apple ID, enable Find My, and give it time to sync." >&2
    exit 1
fi

mkdir -p "$OUT"

# Two password prompts are normal here: the key is non-UTF-8, so `security` asks twice.
echo
echo "Extracting BeaconStore key -- expect one or two keychain password prompts..."
if ! /usr/bin/security find-generic-password -l 'BeaconStore' -w > "$OUT/beaconstore.key" 2>"$OUT/.err"; then
    echo "ERROR: could not read the BeaconStore key:" >&2
    cat "$OUT/.err" >&2
    rm -rf "$OUT"
    exit 1
fi
rm -f "$OUT/.err"

if [ ! -s "$OUT/beaconstore.key" ]; then
    echo "ERROR: BeaconStore key came back empty." >&2
    rm -rf "$OUT"
    exit 1
fi
echo "Got the key ($(wc -c < "$OUT/beaconstore.key" | tr -d ' ') bytes, value not shown)."

echo "Copying searchpartyd records..."
tar -czf "$OUT/searchpartyd.tar.gz" -C "$HOME/Library" com.apple.icloud.searchpartyd

chmod -R go-rwx "$OUT"

echo
echo "Done: $OUT"
echo "  beaconstore.key       the decryption key -- SECRET"
echo "  searchpartyd.tar.gz   encrypted accessory records -- SECRET"
echo
echo "Copy that directory to the findmy-rest machine, then run there:"
echo "  scripts/decrypt_accessories.py <that-directory>"
