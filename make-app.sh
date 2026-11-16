#!/bin/sh
# Builds Bill.app (with icon) on the Desktop. Re-run after moving the project folder.
set -e
DIR="$(cd "$(dirname "$0")" && pwd)"
APP="$HOME/Desktop/Bill.app"
TMP="$(mktemp -d)"

rm -rf "$APP"
mkdir -p "$APP/Contents/MacOS" "$APP/Contents/Resources"

# Icon: render the SVG with macOS-style padding, then build an .icns
sed 's/viewBox="0 0 64 64"/viewBox="-7.75 -7.75 79.5 79.5"/' "$DIR/icon.svg" > "$TMP/icon.svg"
swift "$DIR/render-icon.swift" "$TMP/icon.svg" "$TMP/icon.svg.png" 1024
mkdir "$TMP/AppIcon.iconset"
for s in 16 32 128 256 512; do
  sips -z $s $s "$TMP/icon.svg.png" --out "$TMP/AppIcon.iconset/icon_${s}x${s}.png" >/dev/null
  d=$((s * 2))
  sips -z $d $d "$TMP/icon.svg.png" --out "$TMP/AppIcon.iconset/icon_${s}x${s}@2x.png" >/dev/null
done
iconutil -c icns "$TMP/AppIcon.iconset" -o "$APP/Contents/Resources/AppIcon.icns"

cat > "$APP/Contents/Info.plist" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>CFBundleName</key><string>Bill</string>
  <key>CFBundleDisplayName</key><string>Bill</string>
  <key>CFBundleIdentifier</key><string>local.bill.app</string>
  <key>CFBundleVersion</key><string>1.0</string>
  <key>CFBundlePackageType</key><string>APPL</string>
  <key>CFBundleExecutable</key><string>Bill</string>
  <key>CFBundleIconFile</key><string>AppIcon</string>
  <key>LSUIElement</key><true/>
</dict>
</plist>
PLIST

# Launcher: same logic as Bill.command, with the project path baked in
sed "s|^DIR=.*|DIR=\"$DIR\"|" "$DIR/Bill.command" > "$APP/Contents/MacOS/Bill"
chmod +x "$APP/Contents/MacOS/Bill"

rm -rf "$TMP"
touch "$APP"
echo "Built $APP"
