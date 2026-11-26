#!/usr/bin/env bash
# Build "Free Mail.app" and FreeMail-v<VERSION>-macos-<arch>.dmg (run on a Mac).
#   macos/build-app.sh                       # uses .venv/bin/python
#   PYTHON=/path/to/python macos/build-app.sh
# The app is ad-hoc signed (no Apple Developer ID): a copy downloaded from GitHub needs
# right-click > Open the first time, a build you make locally opens normally.
set -euo pipefail
cd "$(dirname "$0")/.."
PY="${PYTHON:-.venv/bin/python}"
VERSION="$(cat VERSION)"
ARCH="$(uname -m)"; [ "$ARCH" = "arm64" ] || ARCH="x86_64"
APP="dist-mac/Free Mail.app"
DMG="FreeMail-v${VERSION}-macos-${ARCH}.dmg"

"$PY" -m pip install -q -r requirements.txt -r macos/requirements.txt
rm -rf build/mac dist-mac

# icon: .icns from the web icon
ICONSET="$(mktemp -d)/FreeMail.iconset"; mkdir -p "$ICONSET"
for s in 16 32 128 256; do
  sips -z $s $s web/icons/icon-512.png --out "$ICONSET/icon_${s}x${s}.png" >/dev/null
  sips -z $((s*2)) $((s*2)) web/icons/icon-512.png --out "$ICONSET/icon_${s}x${s}@2x.png" >/dev/null
done
cp web/icons/icon-512.png "$ICONSET/icon_512x512.png"
mkdir -p build/mac
iconutil -c icns "$ICONSET" -o build/mac/FreeMail.icns

"$PY" -m PyInstaller --noconfirm --clean --windowed --name "Free Mail" \
  --osx-bundle-identifier fr.jabassou.freemail.mac --icon "$PWD/build/mac/FreeMail.icns" \
  --distpath dist-mac --workpath build/mac/work --specpath build/mac \
  --add-data "$PWD/web:web" --add-data "$PWD/VERSION:." \
  --add-data "$PWD/rules.example.yaml:." --add-data "$PWD/filters.example.yaml:." --add-data "$PWD/config.example.yaml:." \
  --collect-submodules keyring --hidden-import keyring.backends.macOS \
  --exclude-module playwright --exclude-module tkinter \
  macapp.py

PLIST="$APP/Contents/Info.plist"
plutil -replace CFBundleShortVersionString -string "$VERSION" "$PLIST"
plutil -replace CFBundleVersion -string "$VERSION" "$PLIST"
plutil -replace NSHighResolutionCapable -bool true "$PLIST"
codesign --force --deep --sign - "$APP"

STAGE="$(mktemp -d)"; cp -R "$APP" "$STAGE/"; ln -s /Applications "$STAGE/Applications"
rm -f "$DMG"
hdiutil create -volname "Free Mail" -srcfolder "$STAGE" -ov -format UDZO "$DMG" >/dev/null
echo "built: $APP"; echo "built: $DMG"
