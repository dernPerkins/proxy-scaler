#!/usr/bin/env bash
# Assemble a styled drag-to-Applications .dmg from an already-built .app.
#
# The Makefile can't use Tauri's own dmg bundler (it seals the .dmg before
# the sidecar is patched into the .app -- see macos-release-client), and a
# bare `hdiutil create` can't set a background image or icon positions:
# that styling lives in the volume's .DS_Store, which only Finder writes.
# So: stage the volume contents, create a read-write dmg, mount it, drive
# Finder over AppleScript to lay it out, then compress to the final UDZO.
#
# Finder automation needs a logged-in GUI session, and the first run asks
# for permission to control Finder (System Settings > Privacy & Security >
# Automation) -- deny it and the osascript step fails. Same constraint as
# the codesign step: this can only run on a real Mac, not headless CI.
#
# The background png is authored at 2x with 144-dpi metadata; Finder reads
# the dpi and renders it at 1x size, crisp on Retina (create-dmg documents
# this same trick).
set -euo pipefail

if [ $# -ne 6 ]; then
    echo "usage: $0 <app-bundle> <stage-dir> <app-name> <volume-name> <out-dmg> <background-png>" >&2
    exit 1
fi

APP_BUNDLE=$1   # path to the built .app
STAGE=$2        # scratch dir, created and destroyed here
APP_NAME=$3     # filename inside the dmg, e.g. "Proxy Scaler.app"
VOL_NAME=$4     # volume name Finder shows in the title bar
OUT_DMG=$5
BACKGROUND=$6

# Must match the positions the background art's arrow was drawn between
# (desktop/*/dmg/dmg-background.png: app left, Applications right).
WIN_W=660 WIN_H=400
APP_X=180 APP_Y=170
APPS_X=480 APPS_Y=170

RW_DMG="$STAGE.rw.dmg"
MOUNT_DIR="/Volumes/$VOL_NAME"

rm -rf "$STAGE" "$RW_DMG"
mkdir -p "$STAGE/.background" "$(dirname "$OUT_DMG")"
ditto "$APP_BUNDLE" "$STAGE/$APP_NAME"
ln -s /Applications "$STAGE/Applications"
cp "$BACKGROUND" "$STAGE/.background/background.png"

# UDRW (not the final UDZO directly) so Finder can write the .DS_Store.
hdiutil create -volname "$VOL_NAME" -srcfolder "$STAGE" -ov -format UDRW "$RW_DMG"

# A leftover mount with the same volume name (crashed previous run, or the
# user browsing an old dmg) would make the AppleScript talk to the wrong disk.
if [ -d "$MOUNT_DIR" ]; then
    hdiutil detach "$MOUNT_DIR" -force || true
fi
hdiutil attach "$RW_DMG" -readwrite -noverify -noautoopen

cleanup() {
    hdiutil detach "$MOUNT_DIR" -force >/dev/null 2>&1 || true
}
trap cleanup EXIT

# +28 on the height: bounds covers the whole window including the title
# bar, and the background image should fill the 660x400 content area.
osascript <<EOF
tell application "Finder"
    tell disk "$VOL_NAME"
        open
        set current view of container window to icon view
        set toolbar visible of container window to false
        set statusbar visible of container window to false
        set the bounds of container window to {200, 120, $((200 + WIN_W)), $((120 + WIN_H + 28))}
        set opts to the icon view options of container window
        set arrangement of opts to not arranged
        set icon size of opts to 100
        set text size of opts to 13
        set background picture of opts to file ".background:background.png"
        set position of item "$APP_NAME" of container window to {$APP_X, $APP_Y}
        set position of item "Applications" of container window to {$APPS_X, $APPS_Y}
        close
        open
        update without registering applications
        delay 2
        close
    end tell
end tell
EOF

# Let Finder finish flushing the .DS_Store before the volume goes away.
sync
hdiutil detach "$MOUNT_DIR"
trap - EXIT

rm -f "$OUT_DMG"
hdiutil convert "$RW_DMG" -format UDZO -imagekey zlib-level=9 -o "$OUT_DMG"
rm -f "$RW_DMG"
rm -rf "$STAGE"
