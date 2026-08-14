#!/bin/sh
set -eu

model_dir="${PIPER_MODEL_DIR:-/models}"
voice="${PIPER_VOICE:-ko_KR-kss-medium}"

mkdir -p "$model_dir" "${PIPER_OUTPUT_DIR:-/data/audio}"
model_path="$model_dir/$voice.onnx"

if [ "${PIPER_AUTO_DOWNLOAD:-true}" != "false" ] && [ ! -s "$model_path" ]; then
  echo "Downloading Piper voice: $voice"
  python -m piper.download_voices "$voice" --download-dir "$model_dir"
fi

if [ ! -s "$model_path" ] || [ ! -s "$model_path.json" ]; then
  echo "Piper voice is unavailable: $voice" >&2
  exit 1
fi

exec uvicorn server:app --host 0.0.0.0 --port "${TTS_PORT:-8765}"
