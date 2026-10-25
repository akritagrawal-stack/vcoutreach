#!/bin/bash
# Publikreach one-line installer (macOS, Apple Silicon). Read it before you run it.
#   curl -fsSL https://raw.githubusercontent.com/akritagrawal-stack/vcoutreach/main/packaging/install.sh | bash
# Env overrides: PUBLIKREACH_DMG=/path/to.dmg (skip download), PUBLIKREACH_INSTALL_DIR=DIR
# No sudo, no telemetry. The only network call is the dmg download.
set -euo pipefail

URL=https://github.com/akritagrawal-stack/vcoutreach/releases/latest/download/Publikreach.dmg
fail() { printf 'Error: %s\n' "$*" >&2; exit 1; }
[ "$(uname -s)" = Darwin ] || fail 'Publikreach is a Mac app. Run this on macOS.'
[ "$(uname -m)" = arm64 ] || fail 'This build needs an Apple Silicon Mac (arm64). Intel is not supported.'

if [ -n "${PUBLIKREACH_INSTALL_DIR:-}" ]; then
    DEST=$PUBLIKREACH_INSTALL_DIR
elif [ -w /Applications ]; then
    DEST=/Applications
else
    DEST=$HOME/Applications
fi
mkdir -p "$DEST"
[ -w "$DEST" ] || fail "Cannot write to $DEST."

TMP=$(mktemp -d "${TMPDIR:-/tmp}/publikreach-install.XXXXXX")
MNT=$TMP/mnt
cleanup() {
    if [ -d "$MNT" ] && mount | grep -q " $MNT "; then hdiutil detach -quiet "$MNT" || hdiutil detach -force -quiet "$MNT" || true; fi
    rm -rf -- "$TMP"
}
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM

if [ -n "${PUBLIKREACH_DMG:-}" ]; then
    [ -f "$PUBLIKREACH_DMG" ] || fail "No such file: $PUBLIKREACH_DMG"
    DMG=$PUBLIKREACH_DMG
else
    DMG=$TMP/Publikreach.dmg
    echo 'Downloading Publikreach...'
    curl -fsSL -o "$DMG" "$URL" || fail 'Download failed. Check your connection and that a release exists.'
fi

mkdir "$MNT"
hdiutil attach -nobrowse -readonly -quiet -mountpoint "$MNT" "$DMG" >/dev/null || fail 'Could not open the disk image.'
[ -d "$MNT/Publikreach.app" ] || fail 'Publikreach.app is not in the disk image.'

TARGET=$DEST/Publikreach.app
if pgrep -x Publikreach >/dev/null 2>&1; then
    echo 'Quitting the running Publikreach...'
    osascript -e 'tell application "Publikreach" to quit' >/dev/null 2>&1 || true
    sleep 2
    pkill -x Publikreach >/dev/null 2>&1 || true
fi
rm -rf -- "$TARGET"
ditto "$MNT/Publikreach.app" "$TARGET"
xattr -cr "$TARGET"
codesign --verify --deep "$TARGET" || fail 'The installed app failed its signature check. It was not opened.'
echo "Installed to $TARGET"

hdiutil detach -quiet "$MNT" || true
if [ -z "${PUBLIKREACH_INSTALL_DIR:-}" ]; then open "$TARGET"; fi

cat <<MSG

Next steps:
  1. Publikreach opens in TEST mode. Nothing is sent until you turn that off.
  2. In Settings, connect Gmail with an app password (the easy way).
  3. Your data lives in ~/Documents/Publikreach and is kept when you update.
To update, run the same install command again.
MSG
