#!/bin/zsh
set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
APP="$PROJECT_DIR/desktop/BY.app"

[[ -f "$APP/Contents/Info.plist" ]]
[[ -x "$APP/Contents/MacOS/BY" ]]
[[ -f "$APP/Contents/Resources/BY.icns" ]]
/usr/libexec/PlistBuddy -c 'Print :CFBundleDisplayName' "$APP/Contents/Info.plist" | grep -qx 'BY'
/usr/libexec/PlistBuddy -c 'Print :CFBundleIdentifier' "$APP/Contents/Info.plist" | grep -qx 'com.by.personal-work-manager'
BY_PROJECT_DIR="$PROJECT_DIR" BY_DRY_RUN=1 "$APP/Contents/MacOS/BY" | grep -q '^BY ready:'
echo "BY Spotlight bundle checks passed"
