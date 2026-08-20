from __future__ import annotations

import httpx
import pytest
import json
import sqlite3
from fastapi.testclient import TestClient

from app.extraction import DeterministicTestProvider
from app.main import create_app
from app.tts import (
    FailoverTTSBridge,
    LocalTTSBridge,
    TTSResult,
    TTSError,
    TTSTimeout,
)


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
    assert result.provider_name == "local"
    assert result.fallback_used is False


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


def test_local_tts_bridge_rejects_invalid_duration() -> None:
    bridge = LocalTTSBridge(
        base_url="http://bridge:8765",
        public_base_url="http://localhost:8765",
        client=httpx.Client(
            transport=httpx.MockTransport(
                lambda _: httpx.Response(
                    200,
                    json={"audio_url": "/audio/abc123", "duration": "unknown"},
                )
            )
        ),
    )
    with pytest.raises(TTSError):
        bridge.synthesize("응답")


class StubProvider:
    def __init__(
        self,
        *,
        provider_name: str,
        result: TTSResult | None = None,
        error: TTSError | None = None,
    ) -> None:
        self.provider_name = provider_name
        self.model_name = provider_name + "-model"
        self.result = result
        self.error = error
        self.calls = 0

    def synthesize(self, text: str) -> TTSResult:
        assert text
        self.calls += 1
        if self.error is not None:
            raise self.error
        assert self.result is not None
        return self.result


def test_failover_bridge_does_not_call_fallback_after_primary_success() -> None:
    primary = StubProvider(
        provider_name="private",
        result=TTSResult("http://localhost:8765/audio/private", 1.0),
    )
    fallback = StubProvider(
        provider_name="piper",
        result=TTSResult("http://localhost:8766/audio/piper", 1.2),
    )
    bridge = FailoverTTSBridge(primary=primary, fallback=fallback)

    result = bridge.synthesize("응답")

    assert result.audio_url.endswith("/private")
    assert primary.calls == 1
    assert fallback.calls == 0


def test_failover_bridge_uses_piper_once_after_private_timeout() -> None:
    primary = StubProvider(
        provider_name="private",
        error=TTSTimeout("slow"),
    )
    fallback = StubProvider(
        provider_name="piper",
        result=TTSResult(
            "http://localhost:8766/audio/piper",
            1.2,
            provider_name="piper",
            model_name="ko_KR-kss-medium",
        ),
    )
    bridge = FailoverTTSBridge(primary=primary, fallback=fallback)

    result = bridge.synthesize("응답")

    assert result.audio_url == "http://localhost:8766/audio/piper"
    assert result.fallback_used is True
    assert result.primary_error_code == "TTSTimeout"
    assert primary.calls == 1
    assert fallback.calls == 1


@pytest.mark.parametrize("payload", [[], "not-an-object", None])
def test_failover_bridge_uses_piper_after_non_object_json(payload) -> None:
    primary = LocalTTSBridge(
        base_url="http://private:8765",
        public_base_url="http://localhost:8765",
        client=httpx.Client(
            transport=httpx.MockTransport(
                lambda _: httpx.Response(200, json=payload)
            )
        ),
    )
    fallback = StubProvider(
        provider_name="piper",
        result=TTSResult(
            "http://localhost:8766/audio/piper",
            1.2,
            provider_name="piper",
        ),
    )

    result = FailoverTTSBridge(
        primary=primary,
        fallback=fallback,
    ).synthesize("응답")

    assert result.fallback_used is True
    assert result.primary_error_code == "TTSError"
    assert fallback.calls == 1


def test_failover_bridge_propagates_failure_when_both_are_unavailable() -> None:
    bridge = FailoverTTSBridge(
        primary=StubProvider(provider_name="private", error=TTSError("down")),
        fallback=StubProvider(provider_name="piper", error=TTSError("down")),
    )

    with pytest.raises(TTSError):
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


def test_create_app_builds_optional_tts_failover_from_environment(
    tmp_path, monkeypatch
) -> None:
    monkeypatch.setenv("TTS_ENABLED", "true")
    monkeypatch.setenv("TTS_BRIDGE_URL", "http://private:8765")
    monkeypatch.setenv("TTS_PUBLIC_BASE_URL", "http://localhost:8765")
    monkeypatch.setenv("TTS_PROVIDER_NAME", "local-private")
    monkeypatch.setenv("TTS_MODEL_NAME", "private-voice")
    monkeypatch.setenv("TTS_FALLBACK_BRIDGE_URL", "http://piper:8765")
    monkeypatch.setenv("TTS_FALLBACK_PUBLIC_BASE_URL", "http://localhost:8766")

    app = create_app(
        database_path=tmp_path / "tts-config.sqlite",
        extractor=DeterministicTestProvider(),
    )

    assert isinstance(app.state.tts, FailoverTTSBridge)
    assert app.state.tts.primary.base_url == "http://private:8765/"
    assert app.state.tts.fallback.base_url == "http://piper:8765/"
    assert app.state.tts.fallback.public_base_url == "http://localhost:8766/"


class FakeFallbackTTS:
    provider_name = "local-private"
    model_name = "private-voice"

    def synthesize(self, text: str) -> TTSResult:
        assert text
        return TTSResult(
            "http://localhost:8766/audio/fallback",
            1.0,
            provider_name="local-piper",
            model_name="ko_KR-kss-medium",
            fallback_used=True,
            primary_error_code="TTSTimeout",
        )


class FakeUnavailableTTS:
    provider_name = "unavailable"
    model_name = "unavailable"

    def synthesize(self, text: str) -> TTSResult:
        assert text
        raise TTSError("both voice providers are unavailable")


def test_tts_failure_preserves_canonical_work_write(tmp_path) -> None:
    app = create_app(
        database_path=tmp_path / "tts-unavailable.sqlite",
        extractor=DeterministicTestProvider(),
        tts=FakeUnavailableTTS(),
    )
    with TestClient(app) as client:
        payload = {
            "client_message_id": "tts-unavailable-1",
            "content": "오늘 예측매니저 설치 가이드 수정했어.",
        }
        response = client.post(
            "/api/v1/chat/runs",
            json=payload,
        )
        replay = client.post("/api/v1/chat/runs", json=payload)

    assert response.status_code == 200
    assert replay.status_code == 200
    assert response.json()["audio_url"] is None
    with app.state.database.connect() as connection:
        project_count = connection.execute("SELECT COUNT(*) FROM projects").fetchone()[0]
        activity_count = connection.execute("SELECT COUNT(*) FROM activities").fetchone()[0]
        failure_count = connection.execute(
            "SELECT COUNT(*) FROM execution_events WHERE event_type = 'TTS_FAILED'"
        ).fetchone()[0]
        run_status = connection.execute(
            """
            SELECT run.status
            FROM orchestration_runs AS run
            JOIN chat_messages AS message ON message.id = run.request_message_id
            WHERE message.client_message_id = ?
            """,
            (payload["client_message_id"],),
        ).fetchone()[0]
    assert project_count == 1
    assert activity_count == 1
    assert failure_count == 1
    assert run_status == "COMPLETED"


class RunStatusInspectingTTS:
    provider_name = "inspector"
    model_name = "inspector"

    def __init__(self, database_path) -> None:
        self.database_path = database_path
        self.status_seen: str | None = None

    def synthesize(self, text: str) -> TTSResult:
        assert text
        with sqlite3.connect(self.database_path) as connection:
            self.status_seen = connection.execute(
                "SELECT status FROM orchestration_runs ORDER BY started_at DESC LIMIT 1"
            ).fetchone()[0]
        return TTSResult("http://localhost:8765/audio/after-complete", 1.0)


def test_run_is_terminal_before_tts_generation_starts(tmp_path) -> None:
    database_path = tmp_path / "tts-lifecycle.sqlite"
    tts = RunStatusInspectingTTS(database_path)
    app = create_app(
        database_path=database_path,
        extractor=DeterministicTestProvider(),
        tts=tts,
    )

    with TestClient(app) as client:
        response = client.post(
            "/api/v1/chat/runs",
            json={
                "client_message_id": "tts-lifecycle-1",
                "content": "오늘 예측매니저 설치 가이드 수정했어.",
            },
        )

    assert response.status_code == 200
    assert tts.status_seen == "COMPLETED"
    assert response.json()["audio_url"].endswith("/after-complete")


class RunResponseMutatingTTS:
    provider_name = "race-simulator"
    model_name = "race-simulator"

    def __init__(self, database_path) -> None:
        self.database_path = database_path

    def synthesize(self, text: str) -> TTSResult:
        assert text
        with sqlite3.connect(self.database_path) as connection:
            row = connection.execute(
                "SELECT id, result_json FROM orchestration_runs ORDER BY started_at DESC LIMIT 1"
            ).fetchone()
            newer_response = json.loads(row[1])
            newer_response["display_response"] = "더 새로운 응답"
            connection.execute(
                """
                UPDATE orchestration_runs
                SET status = 'COMPLETED', result_json = ?
                WHERE id = ?
                """,
                (
                    json.dumps(newer_response, ensure_ascii=False, separators=(",", ":")),
                    row[0],
                ),
            )
            connection.commit()
        return TTSResult("http://localhost:8765/audio/stale", 1.0)


def test_stale_tts_cannot_overwrite_a_newer_terminal_response(tmp_path) -> None:
    database_path = tmp_path / "tts-response-race.sqlite"
    app = create_app(
        database_path=database_path,
        extractor=DeterministicTestProvider(),
        tts=RunResponseMutatingTTS(database_path),
    )

    with TestClient(app) as client:
        response = client.post(
            "/api/v1/chat/runs",
            json={
                "client_message_id": "tts-response-race-1",
                "content": "오늘 예측매니저 설치 가이드 수정했어.",
            },
        )

    assert response.status_code == 200
    assert response.json()["audio_url"].endswith("/stale")
    with app.state.database.connect() as connection:
        stored = json.loads(
            connection.execute(
                "SELECT result_json FROM orchestration_runs"
            ).fetchone()[0]
        )
    assert stored["display_response"] == "더 새로운 응답"
    assert stored["audio_url"] is None


def test_tts_fallback_event_contains_only_safe_provider_metadata(tmp_path) -> None:
    app = create_app(
        database_path=tmp_path / "tts-fallback.sqlite",
        extractor=DeterministicTestProvider(),
        tts=FakeFallbackTTS(),
    )
    with TestClient(app) as client:
        payload = {
            "client_message_id": "tts-fallback-event-1",
            "content": "오늘 예측매니저 설치 가이드 수정했어.",
        }
        response = client.post(
            "/api/v1/chat/runs",
            json=payload,
        )
        replay = client.post("/api/v1/chat/runs", json=payload)

    assert response.status_code == 200
    assert replay.status_code == 200
    with app.state.database.connect() as connection:
        row = connection.execute(
            """
            SELECT payload_json
            FROM execution_events
            WHERE event_type = 'TTS_FALLBACK_USED'
            """
        ).fetchone()
        activity_count = connection.execute("SELECT COUNT(*) FROM activities").fetchone()[0]
        fallback_event_count = connection.execute(
            "SELECT COUNT(*) FROM execution_events WHERE event_type = 'TTS_FALLBACK_USED'"
        ).fetchone()[0]
    assert row is not None
    assert activity_count == 1
    assert fallback_event_count == 1
    assert json.loads(row["payload_json"]) == {
        "error_code": "TTSTimeout",
        "fallback_provider": "local-piper",
        "primary_provider": "local-private",
    }
