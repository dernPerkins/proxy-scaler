#!/usr/bin/env bash
# Inside-out Developer ID signing of a built .app, sidecar included.
#
# The ad-hoc path (Makefile's codesign_app fallback) only re-seals the
# outer bundle, which is enough to run locally but not to notarize:
# notarization requires every Mach-O in the bundle to carry the Developer
# ID signature, hardened runtime enabled, with a secure timestamp. The
# PyInstaller sidecar's binaries arrive only ad-hoc signed, so each one
# has to be re-signed individually, innermost first, outer .app last.
#
# NEVER add --deep here: it walks PyInstaller's *.dist-info directories,
# mistakes them for bundles, and aborts — see docs/releasing.md. This
# script IS the inside-out replacement Apple recommends over --deep.
#
# Needs the Developer ID Application certificate in the login keychain
# and network access (--timestamp contacts Apple's timestamp service).
set -euo pipefail

if [ $# -lt 2 ]; then
    echo "usage: $0 <app-bundle> <signing-identity> [entitlements.plist]" >&2
    exit 1
fi

APP=$1
IDENTITY=$2
ENTITLEMENTS=${3:-"$(dirname "$0")/macos-entitlements.plist"}

SIDECAR="$APP/Contents/Resources/proxy-scaler-serve"

echo "==> signing shared libraries (.so/.dylib)"
# Batched through xargs: one codesign invocation per 64 files keeps the
# few thousand sidecar libs from taking one process spawn + timestamp
# round-trip each.
find "$APP" -type f \( -name '*.so' -o -name '*.dylib' \) -print0 \
    | xargs -0 -n 64 codesign --force --timestamp --options runtime --sign "$IDENTITY"

if [ -d "$SIDECAR" ]; then
    echo "==> signing sidecar executables (with entitlements)"
    # Anything Mach-O that isn't a shared lib: the python binary,
    # proxy-scaler-serve itself, helper executables. Non-binary files
    # (scripts, data) are skipped by the `file` check; they're covered by
    # the outer bundle's resource seal.
    find "$SIDECAR" -type f ! -name '*.so' ! -name '*.dylib' -print0 \
        | while IFS= read -r -d '' f; do
            if file -b "$f" | grep -q 'Mach-O'; then
                codesign --force --timestamp --options runtime \
                    --entitlements "$ENTITLEMENTS" --sign "$IDENTITY" "$f"
            fi
        done
else
    echo "warning: no sidecar at $SIDECAR -- signing without it" >&2
fi

if [ -d "$APP/Contents/Frameworks" ]; then
    echo "==> signing bundled frameworks"
    find "$APP/Contents/Frameworks" -mindepth 1 -maxdepth 1 -print0 \
        | xargs -0 -n 8 codesign --force --timestamp --options runtime --sign "$IDENTITY"
fi

echo "==> signing the outer bundle"
# No entitlements on the Tauri binary: only the sidecar's CPython needs
# hardened-runtime exceptions.
codesign --force --timestamp --options runtime --sign "$IDENTITY" "$APP"
codesign --verify --strict "$APP"
echo "signed with Developer ID: $APP"
