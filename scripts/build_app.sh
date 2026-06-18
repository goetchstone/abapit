#!/bin/sh
# Build a self-contained abapit.app that bundles its own CPython, so it runs
# on any Mac with nothing pre-installed (no system Python, no Command Line
# Tools). Run on the target arch (or once per arch you ship).
#
#   scripts/build_app.sh            # -> ~/Applications/abapit.app + login service
#   APP_DEST=./dist scripts/build_app.sh   # -> ./dist/abapit.app, no service
#
# The bundled interpreter comes from astral-sh/python-build-standalone (a
# relocatable CPython). The app is NOT code-signed/notarized here — fine for
# the machine that built it; for distribution to other Macs, codesign the
# bundle with a Developer ID and notarize it (see README).
set -eu

PYVER="${PYVER:-3.12}"
PORT="${ABAPIT_PORT:-8866}"
LABEL="io.abapit.serve"
REPO="$(cd "$(dirname "$0")/.." && pwd)"
APP_DEST="${APP_DEST:-$HOME/Applications}"
APP="$APP_DEST/abapit.app"
CT="$APP/Contents"
BUILD="$(mktemp -d)"
trap 'rm -rf "$BUILD"' EXIT

case "$(uname -m)" in
  arm64) TRIPLE=aarch64-apple-darwin ;;
  x86_64) TRIPLE=x86_64-apple-darwin ;;
  *) echo "unsupported arch $(uname -m)" >&2; exit 1 ;;
esac

echo "==> resolving python-build-standalone CPython $PYVER for $TRIPLE"
URL="$(curl -fsSL https://api.github.com/repos/astral-sh/python-build-standalone/releases/latest \
  | grep -o "https://[^\"]*cpython-${PYVER}\.[0-9][0-9]*%2B[0-9]*-${TRIPLE}-install_only\.tar\.gz" \
  | head -1)"
[ -n "$URL" ] || { echo "could not find a CPython $PYVER build for $TRIPLE" >&2; exit 1; }
echo "    $URL"

echo "==> downloading + extracting the interpreter"
curl -fSL "$URL" -o "$BUILD/python.tar.gz"
tar -xzf "$BUILD/python.tar.gz" -C "$BUILD"   # -> $BUILD/python/
PY="$BUILD/python/bin/python3"
"$PY" --version

echo "==> installing abapit + deps into the bundled interpreter"
"$PY" -m pip install --upgrade pip >/dev/null
"$PY" -m pip install "$REPO"          # non-editable: copies abapit into the bundle
"$PY" - <<'PYCHECK'
import abapit, abapit.mosyle
from abapit.web.app import create_app
print("    bundled import OK:", abapit.__version__)
PYCHECK

echo "==> assembling the .app bundle"
rm -rf "$BUILD/abapit.app"
mkdir -p "$BUILD/abapit.app/Contents/MacOS" "$BUILD/abapit.app/Contents/Resources"
mv "$BUILD/python" "$BUILD/abapit.app/Contents/Resources/python"

cat > "$BUILD/abapit.app/Contents/Info.plist" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>CFBundleName</key><string>abapit</string>
  <key>CFBundleDisplayName</key><string>abapit</string>
  <key>CFBundleIdentifier</key><string>io.abapit.app</string>
  <key>CFBundleExecutable</key><string>abapit</string>
  <key>CFBundleVersion</key><string>0.2.0</string>
  <key>CFBundleShortVersionString</key><string>0.2.0</string>
  <key>CFBundlePackageType</key><string>APPL</string>
  <key>LSMinimumSystemVersion</key><string>11.0</string>
</dict></plist>
PLIST

# Launcher: ensure the bundled server is up (start it detached if not), then
# open the browser. The login service normally keeps it running already.
cat > "$BUILD/abapit.app/Contents/MacOS/abapit" <<LAUNCH
#!/bin/sh
HERE="\$(cd "\$(dirname "\$0")/../Resources/python" && pwd)"
URL="http://127.0.0.1:$PORT"
LOG="\$HOME/Library/Logs/abapit.log"
if ! /usr/bin/curl -s -o /dev/null --max-time 1 "\$URL"; then
  nohup "\$HERE/bin/python3" -m abapit.cli serve --no-browser --port $PORT >>"\$LOG" 2>&1 &
  i=0; while [ \$i -lt 40 ]; do /usr/bin/curl -s -o /dev/null --max-time 1 "\$URL" && break; sleep 0.3; i=\$((i+1)); done
fi
exec /usr/bin/open "\$URL"
LAUNCH
chmod +x "$BUILD/abapit.app/Contents/MacOS/abapit"

echo "==> installing to $APP"
mkdir -p "$APP_DEST"
rm -rf "$APP"
# ditto preserves the bundle layout/permissions (cp -R is fine too).
/usr/bin/ditto "$BUILD/abapit.app" "$APP"

if [ "$APP_DEST" = "$HOME/Applications" ]; then
  echo "==> (re)installing the login service pointed at the bundled interpreter"
  PLIST_PATH="$HOME/Library/LaunchAgents/$LABEL.plist"
  BUNDLED_PY="$APP/Contents/Resources/python/bin/python3"
  LOG="$HOME/Library/Logs/abapit.log"
  mkdir -p "$HOME/Library/LaunchAgents" "$HOME/Library/Logs"
  cat > "$PLIST_PATH" <<AGENT
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>$LABEL</string>
  <key>ProgramArguments</key>
  <array>
    <string>$BUNDLED_PY</string><string>-m</string><string>abapit.cli</string>
    <string>serve</string><string>--no-browser</string><string>--port</string><string>$PORT</string>
  </array>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>StandardOutPath</key><string>$LOG</string>
  <key>StandardErrorPath</key><string>$LOG</string>
</dict></plist>
AGENT
  launchctl unload "$PLIST_PATH" 2>/dev/null || true
  launchctl load "$PLIST_PATH"
  echo "    login service $LABEL -> $BUNDLED_PY"
fi

echo "==> done: $APP"
echo "    Bundled Python: $("$APP/Contents/Resources/python/bin/python3" --version 2>&1)"
echo "    The app depends on NO system Python. For other Macs, codesign + notarize it."
