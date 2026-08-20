from __future__ import annotations

from dataclasses import dataclass
import math
from typing import Protocol
from urllib.parse import urljoin

import httpx


class TTSError(RuntimeError):
    """A safe, non-canonical failure from the optional voice presentation layer."""


class TTSTimeout(TTSError):
    pass


@dataclass(frozen=True)
class TTSResult:
    audio_url: str
    duration_seconds: float | None = None
    provider_name: str | None = None
    model_name: str | None = None
    fallback_used: bool = False
    primary_error_code: str | None = None


class TTSProvider(Protocol):
    """Presentation-only voice provider used by the orchestrator."""

    provider_name: str
    model_name: str

    def synthesize(self, text: str) -> TTSResult: ...


class LocalTTSBridge:
    """Adapter for a separately managed local TTS service.

    The service owns model download/loading. BY only receives a browser-safe
    URL, so TTS never becomes a Canonical Memory write path. Keeping this
    adapter HTTP-based also leaves room for a hosted TTS provider later.
    """

    provider_name = "local"
    model_name = "unknown"

    def __init__(
        self,
        *,
        base_url: str,
        public_base_url: str,
        timeout_seconds: float = 120.0,
        provider_name: str = "local",
        model_name: str = "unknown",
        client: httpx.Client | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/") + "/"
        self.public_base_url = public_base_url.rstrip("/") + "/"
        self.timeout_seconds = timeout_seconds
        self.provider_name = provider_name
        self.model_name = model_name
        self._client = client

    def synthesize(self, text: str) -> TTSResult:
        if not text or len(text) > 300:
            raise TTSError("voice text is outside the safe length limit")
        owns_client = self._client is None
        client = self._client or httpx.Client(timeout=self.timeout_seconds)
        try:
            try:
                response = client.post(
                    urljoin(self.base_url, "api/generate"),
                    json={"text": text, "speed": 1.0, "pitch": 0},
                    timeout=self.timeout_seconds,
                )
            except httpx.TimeoutException as exc:
                raise TTSTimeout("TTS service timed out") from exc
            except httpx.HTTPError as exc:
                raise TTSError("TTS service is unavailable") from exc
            if response.status_code >= 400:
                raise TTSError(f"TTS service returned HTTP {response.status_code}")
            try:
                payload = response.json()
            except ValueError as exc:
                raise TTSError("TTS service returned invalid JSON") from exc
            if not isinstance(payload, dict):
                raise TTSError("TTS service returned a non-object JSON payload")
            relative_url = payload.get("audio_url")
            if not isinstance(relative_url, str) or not relative_url.startswith("/"):
                raise TTSError("TTS service did not return an audio URL")
            duration = payload.get("duration")
            if duration is not None:
                try:
                    duration = float(duration)
                except (TypeError, ValueError) as exc:
                    raise TTSError("TTS service returned an invalid duration") from exc
                if not math.isfinite(duration) or duration < 0:
                    raise TTSError("TTS service returned an invalid duration")
            return TTSResult(
                audio_url=urljoin(self.public_base_url, relative_url.lstrip("/")),
                duration_seconds=duration,
                provider_name=self.provider_name,
                model_name=self.model_name,
            )
        finally:
            if owns_client:
                client.close()


class FailoverTTSBridge:
    """Try a private/local voice first and fall back once to a public default.

    Both providers remain outside Canonical Memory. A failure never retries the
    work operation; only the presentation-only synthesis request is retried.
    """

    def __init__(self, *, primary: TTSProvider, fallback: TTSProvider) -> None:
        self.primary = primary
        self.fallback = fallback
        self.provider_name = primary.provider_name
        self.model_name = primary.model_name

    def synthesize(self, text: str) -> TTSResult:
        try:
            return self.primary.synthesize(text)
        except TTSError as primary_error:
            result = self.fallback.synthesize(text)
            return TTSResult(
                audio_url=result.audio_url,
                duration_seconds=result.duration_seconds,
                provider_name=result.provider_name or self.fallback.provider_name,
                model_name=result.model_name or self.fallback.model_name,
                fallback_used=True,
                primary_error_code=type(primary_error).__name__,
            )
