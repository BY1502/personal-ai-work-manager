#!/bin/sh
set -eu

repository_root=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
cd "$repository_root"

if git grep -nE '(/Users/|/home/[^/]+/|C:\\Users\\)' -- . \
  ':(exclude)scripts/check_public_safety.sh'; then
  echo "Public-safety check failed: tracked absolute user path found." >&2
  exit 1
fi

tracked_private_artifacts=$(
  git ls-files | grep -Ei \
    '(^|/)(\.local|private)/|\.mlmodelc/|\.(npz|safetensors|onnx|onnx\.json|gguf|ckpt|pt|pth|bin|npy|mlmodel|wav|mp3|flac|m4a|provenance\.json)$' \
    || true
)
if [ -n "$tracked_private_artifacts" ]; then
  echo "Public-safety check failed: private model/audio artifact is tracked." >&2
  echo "$tracked_private_artifacts" >&2
  exit 1
fi

for ignored_path in \
  .env \
  .local/tts/private-voice.wav \
  private/model.safetensors \
  compose.local.yaml \
  tts/local.env \
  tts/audio/generated.wav \
  tts/output.provenance.json
do
  if ! git check-ignore --quiet --no-index "$ignored_path"; then
    echo "Public-safety check failed: $ignored_path is not ignored." >&2
    exit 1
  fi
done

echo "Public-safety check passed."
