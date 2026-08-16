#!/usr/bin/env bash
# Sign a finished .dmg, then notarize and staple it.
#
# Runs after build-dmg-macos.sh, only when the Makefile is given
# MACOS_SIGN_IDENTITY. The .app inside must already be Developer-ID
# signed (sign-macos.sh) or the notary service will reject the upload.
#
# The notary profile is a keychain item created once per machine with:
#   xcrun notarytool store-credentials <profile-name> \
#       --apple-id <appleid email> --team-id <TEAMID> \
#       --password <app-specific password from appleid.apple.com>
# Without a profile argument the dmg is signed but not notarized --
# fine for local testing, not for anything users download.
set -euo pipefail

if [ $# -lt 2 ]; then
    echo "usage: $0 <dmg> <signing-identity> [notary-profile]" >&2
    exit 1
fi

DMG=$1
IDENTITY=$2
PROFILE=${3:-}

echo "==> signing the dmg"
codesign --force --timestamp --sign "$IDENTITY" "$DMG"

if [ -z "$PROFILE" ]; then
    echo "==> no notary profile given; dmg signed but NOT notarized"
    echo "    (downloads will still hit the Gatekeeper warning -- set"
    echo "     MACOS_NOTARY_PROFILE for release builds)"
    exit 0
fi

echo "==> submitting to Apple's notary service (typically a few minutes)"
SUBMIT_OUT=$(xcrun notarytool submit "$DMG" --keychain-profile "$PROFILE" --wait 2>&1 | tee /dev/stderr)

# notarytool's exit code doesn't reliably distinguish Accepted from
# Invalid, so gate on the printed status.
if ! grep -q 'status: Accepted' <<<"$SUBMIT_OUT"; then
    SUB_ID=$(grep -m1 -Eo 'id: [0-9a-f-]+' <<<"$SUBMIT_OUT" | awk '{print $2}' || true)
    echo "error: notarization not accepted; full report:" >&2
    echo "  xcrun notarytool log ${SUB_ID:-<submission-id>} --keychain-profile $PROFILE" >&2
    exit 1
fi

echo "==> stapling the notarization ticket"
xcrun stapler staple "$DMG"
xcrun stapler validate "$DMG"
echo "notarized and stapled: $DMG"
