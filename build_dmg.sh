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

say "Staging the disk image"
rm -rf "$STAGING" "$DMG"
mkdir -p "$STAGING"
cp -R "$APP" "$STAGING/"
ln -s /Applications "$STAGING/Applications"

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

say "Creating $DMG"
hdiutil create \
  -volname "$VOLUME" \
  -srcfolder "$STAGING" \
  -ov -format UDZO \
  -fs HFS+ \
  "$DMG" >/dev/null

rm -rf "$STAGING"
SIZE="$(du -h "$DMG" | cut -f1)"
say "Done. $DMG ($SIZE)"
echo
echo "  Test it with:  open \"$DMG\""
echo "  Users drag the app onto Applications and eject."
