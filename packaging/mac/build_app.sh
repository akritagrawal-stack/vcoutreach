#!/bin/bash
# Build on a real Apple Silicon Mac. No git operations or target Python install.
set -euo pipefail

fail() { printf '%s\n' "$*" >&2; exit 1; }
[ "$(uname -s)" = Darwin ] || fail 'Build Publikreach on macOS.'
[ "$(uname -m)" = arm64 ] || fail 'Use a native arm64 shell on Apple Silicon, without Rosetta.'
REPO_ROOT=$(cd "$(dirname "$0")/../.." && pwd)
BUILD_PYTHON=${PYTHON:-python3}
"$BUILD_PYTHON" -c 'import platform, sys; assert platform.machine() == "arm64" and (3, 10) <= sys.version_info < (3, 15), "Use native arm64 Python 3.10 through 3.14"'
BUILD_TEMP=$(mktemp -d "${TMPDIR:-/tmp}/publikreach-build.XXXXXX")
cleanup() { rm -rf -- "$BUILD_TEMP"; }
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM

export MACOSX_DEPLOYMENT_TARGET=12.0
export PYTHONNOUSERSITE=1
unset PYTHONPATH PYTHONHOME
"$BUILD_PYTHON" -m venv "$BUILD_TEMP/venv"
BUILD_BIN="$BUILD_TEMP/venv/bin"
"$BUILD_BIN/python" -m pip install --upgrade pip
"$BUILD_BIN/python" -m pip install "$REPO_ROOT[web,gmail]" 'pywebview>=6.0,<7' 'pyinstaller>=6.0,<7'
"$BUILD_BIN/python" "$REPO_ROOT/packaging/mac/app/make_icon.py"
cd "$REPO_ROOT"
"$BUILD_BIN/python" -m PyInstaller --noconfirm --clean \
    --workpath "$BUILD_TEMP/work" --distpath "$REPO_ROOT/dist" \
    "$REPO_ROOT/packaging/mac/app/Publikreach.spec"

APP="$REPO_ROOT/dist/Publikreach.app"
codesign --force --deep -s - "$APP"
codesign --verify --deep --strict "$APP"
# Check the frozen executable and its bundled data before creating the image.
"$APP/Contents/MacOS/Publikreach" --self-test
mkdir "$BUILD_TEMP/image"
ditto "$APP" "$BUILD_TEMP/image/Publikreach.app"
ln -s /Applications "$BUILD_TEMP/image/Applications"
hdiutil create -volname 'Publikreach' -srcfolder "$BUILD_TEMP/image" \
    -format UDZO -ov "$REPO_ROOT/dist/Publikreach.dmg"
printf '%s\n' 'Built dist/Publikreach.app and dist/Publikreach.dmg (ad-hoc signed, not notarized).'
