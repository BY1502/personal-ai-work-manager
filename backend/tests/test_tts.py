from __future__ import annotations

import httpx
import pytest
import json
from fastapi.testclient import TestClient

from app.extraction import DeterministicTestProvider
from app.main import create_app
from app.tts import LocalTTSBridge, TTSResult, TTSError, TTSTimeout


def test_local_tts_bridge_maps_relative_audio_url() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/generate"
        assert json.loads(request.content)["text"] == "업무를 기록했습니다."
        return httpx.Response(
            200,
            json={"audio_url": "/audio/abc123", "duration": 1.25},
        )

    bridge = LocalTTSBridge(
        base_url="http://bridge:8765",
        public_base_url="http://localhost:8765",
        client=httpx.Client(transport=httpx.MockTransport(handler)),
    )
    result = bridge.synthesize("업무를 기록했습니다.")
    assert result.audio_url == "http://localhost:8765/audio/abc123"
    assert result.duration_seconds == 1.25


def test_local_tts_bridge_rejects_invalid_audio_payload() -> None:
    bridge = LocalTTSBridge(
        base_url="http://bridge:8765",
        public_base_url="http://localhost:8765",
        client=httpx.Client(
            transport=httpx.MockTransport(lambda _: httpx.Response(200, json={}))
        ),
    )
    with pytest.raises(TTSError):
        bridge.synthesize("응답")


def test_local_tts_bridge_normalizes_timeout() -> None:
    def handler(_: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("slow", request=httpx.Request("POST", "http://bridge"))

    bridge = LocalTTSBridge(
        base_url="http://bridge:8765",
        public_base_url="http://localhost:8765",
        client=httpx.Client(transport=httpx.MockTransport(handler)),
    )
    with pytest.raises(TTSTimeout):
        bridge.synthesize("응답")


class FakeTTS:
    def synthesize(self, text: str) -> TTSResult:
        assert text
        return TTSResult("http://localhost:8765/audio/fake", 1.0)


def test_tts_is_persisted_after_canonical_apply(tmp_path) -> None:
    app = create_app(
        database_path=tmp_path / "tts.sqlite",
        extractor=DeterministicTestProvider(),
        tts=FakeTTS(),
    )
    with TestClient(app) as client:
        response = client.post(
            "/api/v1/chat/runs",
            json={
                "client_message_id": "tts-persist-1",
                "content": "오늘 예측매니저 설치 가이드 수정했어.",
            },
        )
    assert response.status_code == 200
    assert response.json()["audio_url"] == "http://localhost:8765/audio/fake"
