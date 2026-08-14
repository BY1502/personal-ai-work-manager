from __future__ import annotations

import os
import secrets
import threading
import wave
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field
from piper import PiperVoice, SynthesisConfig


MODEL_DIR = Path(os.getenv("PIPER_MODEL_DIR", "/models"))
OUTPUT_DIR = Path(os.getenv("PIPER_OUTPUT_DIR", "/data/audio"))
VOICE = os.getenv("PIPER_VOICE", "ko_KR-kss-medium")
VOICE_PATH = MODEL_DIR / f"{VOICE}.onnx"
MODEL_LABEL = f"piper/{VOICE}"

app = FastAPI(title="BY Downloaded TTS", version="1.0.0")
allowed_origins = [
    item.strip()
    for item in os.getenv(
        "TTS_ALLOWED_ORIGINS",
        "http://localhost:3100,http://127.0.0.1:3100,http://localhost:3001,http://127.0.0.1:3001",
    ).split(",")
    if item.strip()
]
app.add_middleware(
    CORSMiddleware,
    allow_origins=allowed_origins,
    allow_credentials=False,
    allow_methods=["GET", "POST", "OPTIONS"],
    allow_headers=["Content-Type"],
)


class GenerateRequest(BaseModel):
    text: str = Field(min_length=1, max_length=300)
    speed: float = Field(default=1.0, ge=0.75, le=1.30)
    # Kept for compatibility with the existing Adapter. Piper's stable
    # voice model does not expose pitch shifting, so non-zero values are
    # rejected instead of silently pretending to apply them.
    pitch: int = Field(default=0, ge=-5, le=5)


_voice: PiperVoice | None = None
_voice_lock = threading.Lock()
_generation_lock = threading.Lock()


def get_voice() -> PiperVoice:
    global _voice
    if _voice is None:
        with _voice_lock:
            if _voice is None:
                if not VOICE_PATH.is_file():
                    raise RuntimeError("Piper voice model is not installed")
                _voice = PiperVoice.load(VOICE_PATH)
    return _voice


@app.get("/api/health")
def health() -> dict[str, str | bool]:
    return {
        "ok": VOICE_PATH.is_file() and VOICE_PATH.with_suffix(".onnx.json").is_file(),
        "provider": "piper",
        "model": MODEL_LABEL,
    }


@app.post("/api/generate")
def generate(request: GenerateRequest) -> dict[str, float | str]:
    if request.pitch != 0:
        raise HTTPException(status_code=422, detail="Piper backend only supports pitch=0")
    if not _generation_lock.acquire(blocking=False):
        raise HTTPException(status_code=409, detail="another synthesis is in progress")
    try:
        voice = get_voice()
        item_id = secrets.token_hex(6)
        output_path = OUTPUT_DIR / f"{item_id}.wav"
        OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
        # Piper's length_scale is the inverse of playback speed: a larger
        # value produces slower speech.
        config = SynthesisConfig(length_scale=1.0 / request.speed)
        chunks = voice.synthesize(request.text, config)
        first = next(chunks, None)
        if first is None:
            raise HTTPException(status_code=502, detail="Piper produced no audio")
        with wave.open(str(output_path), "wb") as audio:
            audio.setnchannels(first.sample_channels)
            audio.setsampwidth(first.sample_width)
            audio.setframerate(first.sample_rate)
            audio.writeframes(first.audio_int16_bytes)
            frames = len(first.audio_int16_array)
            for chunk in chunks:
                audio.writeframes(chunk.audio_int16_bytes)
                frames += len(chunk.audio_int16_array)
        duration = frames / first.sample_rate
        return {
            "audio_url": f"/audio/{item_id}",
            "duration": duration,
            "provider": "piper",
            "model": MODEL_LABEL,
        }
    finally:
        _generation_lock.release()


@app.get("/audio/{item_id}")
def audio(item_id: str) -> FileResponse:
    if len(item_id) != 12 or any(char not in "0123456789abcdef" for char in item_id):
        raise HTTPException(status_code=404, detail="audio not found")
    path = OUTPUT_DIR / f"{item_id}.wav"
    if not path.is_file():
        raise HTTPException(status_code=404, detail="audio not found")
    return FileResponse(path, media_type="audio/wav")
