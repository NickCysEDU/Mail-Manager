#!/usr/bin/env bash
# Build a drag-to-install disk image from the app bundle.
#
#   ./build_dmg.sh              # build the app first if needed, then the .dmg
#   ./build_dmg.sh --skip-build # package an app that is already built
#
set -euo pipefail
cd "$(dirname "$0")"

APP_NAME="Mail Manager"
APP="dist/${APP_NAME}.app"
VOLUME="${APP_NAME}"
STAGING="build/dmg"
DMG="dist/${APP_NAME}.dmg"

G=$'\033[32m'; R=$'\033[31m'; N=$'\033[0m'
say()  { printf '%s==>%s %s\n' "$G" "$N" "$*" >&2; }
die()  { printf '%serror:%s %s\n' "$R" "$N" "$*" >&2; exit 1; }

if [[ "${1:-}" != "--skip-build" ]]; then
  say "Building the app"
  ./build_app.sh --skip-tests
fi
[[ -d "$APP" ]] || die "$APP not found. Run ./build_app.sh first."

# Report what is being packaged, since an app built for one architecture looks
# identical to one built for both until somebody on the other kind opens it.
ARCHS="$(lipo -archs "$APP/Contents/MacOS/$APP_NAME" 2>/dev/null || echo unknown)"
SIGNED_BY="$(codesign -dvv "$APP" 2>&1 | sed -n 's/^Authority=//p' | head -1)"
say "Packaging $ARCHS${SIGNED_BY:+, signed by $SIGNED_BY}"
case "$ARCHS" in
  *arm64*x86_64*|*x86_64*arm64*) ;;
  *) say "  This is a single-architecture build. For both:" 
     say "    ./tools/fetch_universal_python.sh && ./build_app.sh" ;;
esac

BACKGROUND="assets/dmg-background.tiff"
[[ -f "$BACKGROUND" ]] || BACKGROUND="assets/dmg-background.png"

say "Staging the disk image"
rm -rf "$STAGING" "$DMG" "$DMG.tmp.dmg"
mkdir -p "$STAGING/.background"
cp -R "$APP" "$STAGING/"
ln -s /Applications "$STAGING/Applications"
[[ -f "$BACKGROUND" ]] && cp "$BACKGROUND" "$STAGING/.background/background.${BACKGROUND##*.}"

# A short note so the window is not just two bare icons.
cat > "$STAGING/Read me first.txt" <<'TXT'
Mail Manager

To install: drag the Mail Manager icon onto the Applications folder beside it,
then eject this disk image and open Mail Manager from Launchpad or Spotlight.

The first launch walks you through connecting your iCloud mailbox. You do not
need an API key: the built-in sorter runs on your Mac and costs nothing.

If macOS says the app is from an unidentified developer, right-click it in
Applications and choose Open. That is only needed once, and only because this
build is not signed with a paid Apple Developer certificate.

Source and documentation: https://github.com/NickCysEDU/Mail-Manager
TXT

# Built read-write first so Finder can be told how to lay the window out,
# then compressed. A UDZO image cannot be styled after the fact.
say "Creating the writable image"
hdiutil create \
  -volname "$VOLUME" \
  -srcfolder "$STAGING" \
  -ov -format UDRW \
  -fs HFS+ \
  "$DMG.tmp.dmg" >/dev/null

say "Laying out the installer window"
MOUNT="$(hdiutil attach -readwrite -noverify -noautoopen "$DMG.tmp.dmg" |
         grep -Eo '/Volumes/.*$' | head -1)"
if [[ -z "$MOUNT" ]]; then
  die "could not mount the staged image"
fi

# Finder scripting needs automation permission the first time, and the prompt
# only appears in a normal desktop session. If it is refused the image is still
# perfectly usable, just without the styled window, so this warns rather than
# failing the build.
BG_NAME="background.${BACKGROUND##*.}"
if ! osascript <<APPLESCRIPT >/dev/null 2>&1
with timeout of 60 seconds
tell application "Finder"
  tell disk "$VOLUME"
    open
    set current view of container window to icon view
    set toolbar visible of container window to false
    set statusbar visible of container window to false
    set the bounds of container window to {200, 140, 860, 560}
    set theOptions to the icon view options of container window
    set arrangement of theOptions to not arranged
    set icon size of theOptions to 128
    set text size of theOptions to 13
    set background picture of theOptions to file ".background:$BG_NAME"
    set position of item "$APP_NAME.app" of container window to {170, 214}
    set position of item "Applications" of container window to {490, 214}
    set position of item "Read me first.txt" of container window to {330, 350}
    close
    open
    update without registering applications
    delay 1
  end tell
end tell
end timeout
APPLESCRIPT
then
  say "Could not style the installer window."
  say "  The disk image is still complete and installable; it just opens as a"
  say "  plain folder rather than the drag-across layout."
  say "  macOS asks for permission the first time a script drives Finder. If no"
  say "  prompt appeared, allow your terminal under System Settings ->"
  say "  Privacy & Security -> Automation -> Finder, then run this again."
fi

chmod -Rf go-w "$MOUNT" 2>/dev/null || true
sync
hdiutil detach "$MOUNT" >/dev/null

say "Compressing $DMG"
hdiutil convert "$DMG.tmp.dmg" -format UDZO -imagekey zlib-level=9 -o "$DMG" >/dev/null
rm -f "$DMG.tmp.dmg"
rm -rf "$STAGING"
SIZE="$(du -h "$DMG" | cut -f1)"
say "Done. $DMG ($SIZE)"
echo
echo "  Test it with:  open \"$DMG\""
echo "  Users drag the app onto Applications and eject."
