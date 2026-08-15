#!/usr/bin/env bash
# Stages and builds the proxy-scaler .deb.
#
# Expects the PyInstaller onedir bundle to already exist at
# desktop/pyinstaller/dist/proxy-scaler-serve/ (i.e. `make sidecar` has
# run). PyInstaller output is platform-specific, so this only produces a
# usable package when run on Linux, on the same architecture you're
# targeting — you cannot build the .deb from macOS.
#
# PROXY_SCALER_DEB_STUB=1 stages a placeholder payload instead of the real
# bundle. That exists to exercise the packaging itself (control fields,
# maintainer scripts, dpkg-deb) without waiting on a multi-minute torch
# freeze; the resulting package will NOT run.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
BUNDLE="$ROOT/desktop/pyinstaller/dist/proxy-scaler-serve"
OUT_DIR="$ROOT/dist"
# Staging needs a filesystem with real Unix permissions: dpkg-deb refuses
# to build from a DEBIAN directory looser than 0775, and on a mount that
# ignores chmod (a repo checkout on NTFS/exFAT via FUSE, a shared VM
# folder, some WSL setups) every directory reads as 0777 and the build
# simply can't succeed — regardless of what this script chmods it to.
# Defaulting to a /tmp-based path sidesteps that outright: nothing about
# the resulting package depends on where it was assembled, and $ROOT
# itself is exactly the kind of checkout that's likely to be on such a
# mount. Set PROXY_SCALER_DEB_STAGE to override.
STAGE_ROOT="${PROXY_SCALER_DEB_STAGE:-${TMPDIR:-/tmp}/proxy-scaler-deb-stage}"

VERSION="$(sed -n 's/^version = "\(.*\)"/\1/p' "$ROOT/pyproject.toml" | head -1)"
ARCH="$(dpkg --print-architecture)"
[ -n "$VERSION" ] || { echo "error: couldn't read version from pyproject.toml" >&2; exit 1; }

STAGE="$STAGE_ROOT/proxy-scaler_${VERSION}_${ARCH}"
rm -rf "$STAGE"
mkdir -p "$STAGE/DEBIAN" \
         "$STAGE/opt/proxy-scaler" \
         "$STAGE/usr/bin" \
         "$STAGE/lib/systemd/system" \
         "$STAGE/etc/default" \
         "$OUT_DIR"

if [ "${PROXY_SCALER_DEB_STUB:-0}" = "1" ]; then
  echo "STUB BUILD — payload is a placeholder, the package will not run."
  printf '#!/bin/sh\necho "stub build" >&2\nexit 1\n' > "$STAGE/opt/proxy-scaler/proxy-scaler-serve"
  chmod 755 "$STAGE/opt/proxy-scaler/proxy-scaler-serve"
  mkdir -p "$STAGE/opt/proxy-scaler/_internal"
  : > "$STAGE/opt/proxy-scaler/_internal/.placeholder"
else
  [ -d "$BUNDLE" ] || {
    echo "error: $BUNDLE not found — run 'make sidecar' first." >&2
    exit 1
  }
  # -L dereferences symlinks: PyInstaller's onedir output carries
  # versioned .so symlinks, and a package of plain regular files is one
  # less thing for dpkg (and anyone inspecting it) to get clever about.
  rsync -aL --delete "$BUNDLE/" "$STAGE/opt/proxy-scaler/"
fi

install -m 755 "$ROOT/packaging/debian/proxy-scaler-serve" "$STAGE/usr/bin/proxy-scaler-serve"
install -m 644 "$ROOT/packaging/debian/proxy-scaler.service" \
               "$STAGE/lib/systemd/system/proxy-scaler.service"
install -m 644 "$ROOT/packaging/debian/default" "$STAGE/etc/default/proxy-scaler"

sed -e "s/@VERSION@/$VERSION/" -e "s/@ARCH@/$ARCH/" \
    "$ROOT/packaging/debian/control.in" > "$STAGE/DEBIAN/control"

# Marks the config as user-editable so dpkg prompts instead of silently
# overwriting an admin's host/port changes on upgrade.
echo "/etc/default/proxy-scaler" > "$STAGE/DEBIAN/conffiles"

for script in postinst prerm postrm; do
  install -m 755 "$ROOT/packaging/debian/$script" "$STAGE/DEBIAN/$script"
done

# Strip group/other write bits across the staged tree. Whatever the build
# checkout happens to be (a world-writable mount, a permissive umask),
# packaged files should not be. dpkg-deb also refuses outright to build
# from a DEBIAN directory looser than 0775.
chmod -R go-w "$STAGE"

# --root-owner-group avoids needing fakeroot just to get root:root
# ownership in the archive.
dpkg-deb --build --root-owner-group "$STAGE" "$OUT_DIR/proxy-scaler_${VERSION}_${ARCH}.deb"

echo
echo "built: $OUT_DIR/proxy-scaler_${VERSION}_${ARCH}.deb"
