#!/bin/zsh
set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
SOURCE_APP="$PROJECT_DIR/desktop/BY.app"
TARGET_DIR="${HOME}/Applications"
TARGET_APP="$TARGET_DIR/BY.app"

if [[ ! -x "$SOURCE_APP/Contents/MacOS/BY" ]]; then
  echo "BY.app executable is missing: $SOURCE_APP/Contents/MacOS/BY" >&2
  exit 1
fi

/bin/mkdir -p "$TARGET_DIR"
if [[ -e "$TARGET_APP" ]]; then
  echo "이미 $TARGET_APP 이 있습니다. 기존 앱을 보존하기 위해 설치를 중단합니다." >&2
  echo "삭제 대신 기존 앱을 다른 이름으로 옮긴 뒤 다시 실행하세요." >&2
  exit 2
fi

/usr/bin/ditto "$SOURCE_APP" "$TARGET_APP"
/bin/mkdir -p "$TARGET_APP/Contents/Resources"
printf '%s\n' "$PROJECT_DIR" > "$TARGET_APP/Contents/Resources/project-path"
/bin/chmod +x "$TARGET_APP/Contents/MacOS/BY"

if command -v mdimport >/dev/null 2>&1; then
  mdimport "$TARGET_APP" >/dev/null 2>&1 || true
fi

echo "설치 완료: $TARGET_APP"
echo "Spotlight(⌘Space)에서 BY를 검색해 실행하세요."
