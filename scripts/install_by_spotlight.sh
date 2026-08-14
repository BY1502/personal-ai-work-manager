#!/bin/zsh
set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
SOURCE_APP="$PROJECT_DIR/desktop/BY.app"
TARGET_DIR="${HOME}/Applications"
TARGET_APP="$TARGET_DIR/BY.app"
UPDATE=0
if [[ "${1:-}" == "--update" ]]; then
  UPDATE=1
fi

if [[ ! -x "$SOURCE_APP/Contents/MacOS/BY" ]]; then
  echo "BY.app executable is missing: $SOURCE_APP/Contents/MacOS/BY" >&2
  exit 1
fi

/bin/mkdir -p "$TARGET_DIR"
if [[ -e "$TARGET_APP" ]]; then
  if [[ "$UPDATE" -ne 1 ]]; then
    echo "이미 $TARGET_APP 이 있습니다. 기존 앱을 보존하기 위해 설치를 중단합니다." >&2
    echo "업데이트하려면 다음을 실행하세요: $0 --update" >&2
    exit 2
  fi
fi

staging_dir="$(/usr/bin/mktemp -d -t by-install)"
trap '/bin/rm -rf "$staging_dir"' EXIT
staged_app="$staging_dir/BY.app"
/usr/bin/ditto "$SOURCE_APP" "$staged_app"
/bin/mkdir -p "$staged_app/Contents/Resources"
printf '%s\n' "$PROJECT_DIR" > "$staged_app/Contents/Resources/project-path"
/bin/chmod +x "$staged_app/Contents/MacOS/BY"
if command -v codesign >/dev/null 2>&1; then
  # Ad-hoc signing avoids an unnecessary first-launch warning for this local app.
  /usr/bin/xattr -cr "$staged_app" 2>/dev/null || true
  codesign --force --deep --sign - "$staged_app" >/dev/null 2>&1 || true
fi

if [[ -e "$TARGET_APP" ]]; then
  backup_dir="$HOME/Library/Application Support/BY/previous"
  /bin/mkdir -p "$backup_dir"
  backup_app="$backup_dir/BY-$(/bin/date +%Y%m%d%H%M%S).app"
  /bin/mv "$TARGET_APP" "$backup_app"
fi
/bin/mv "$staged_app" "$TARGET_APP"

if command -v mdimport >/dev/null 2>&1; then
  mdimport "$TARGET_APP" >/dev/null 2>&1 || true
fi

echo "설치 완료: $TARGET_APP"
echo "Spotlight(⌘Space)에서 BY를 검색해 실행하세요."
