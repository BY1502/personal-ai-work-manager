from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from app.main import create_app
from app.providers import (
    ExtractionInvalidJsonError,
    ExtractionTimeoutError,
    ExtractionTransportError,
    LocalLLMProvider,
    OllamaStructuredOutputTransport,
)


def _ollama_response(content: str, request: httpx.Request) -> httpx.Response:
    return httpx.Response(
        200,
        request=request,
        json={"message": {"content": content}},
    )


def _transport(handler):
    return httpx.Client(
        transport=httpx.MockTransport(handler),
        timeout=0.2,
    )


def test_local_transport_retries_timeout_then_succeeds() -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise httpx.ReadTimeout("simulated slow Ollama", request=request)
        return _ollama_response('{"ok":true}', request)

    transport = OllamaStructuredOutputTransport(
        base_url="http://ollama.test",
        model_name="qwen3.5:35b-a3b-q4_K_M",
        timeout_seconds=0.2,
        client=_transport(handler),
        retry_attempts=2,
        retry_backoff_seconds=0,
    )

    result = transport.complete_json(
        schema_name="test",
        schema={"type": "object"},
        system_prompt="system",
        user_prompt="user",
    )

    assert result == '{"ok":true}'
    assert calls == 2


def test_local_transport_retries_transient_http_status_only() -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls == 1:
            return httpx.Response(503, request=request)
        return _ollama_response('{"ok":true}', request)

    transport = OllamaStructuredOutputTransport(
        base_url="http://ollama.test",
        model_name="qwen3.5:35b-a3b-q4_K_M",
        timeout_seconds=0.2,
        client=_transport(handler),
        retry_attempts=2,
        retry_backoff_seconds=0,
    )

    assert transport.complete_json(
        schema_name="test",
        schema={"type": "object"},
        system_prompt="system",
        user_prompt="user",
    ) == '{"ok":true}'
    assert calls == 2


def test_local_transport_does_not_retry_non_transient_http_status() -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(401, request=request)

    transport = OllamaStructuredOutputTransport(
        base_url="http://ollama.test",
        model_name="qwen3.5:35b-a3b-q4_K_M",
        timeout_seconds=0.2,
        client=_transport(handler),
        retry_attempts=3,
        retry_backoff_seconds=0,
    )

    with pytest.raises(ExtractionTransportError, match="HTTP 401"):
        transport.complete_json(
            schema_name="test",
            schema={"type": "object"},
            system_prompt="system",
            user_prompt="user",
        )
    assert calls == 1


def test_local_transport_final_timeout_is_safe_and_bounded() -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        raise httpx.ReadTimeout("simulated timeout", request=request)

    transport = OllamaStructuredOutputTransport(
        base_url="http://ollama.test",
        model_name="qwen3.5:35b-a3b-q4_K_M",
        timeout_seconds=0.2,
        client=_transport(handler),
        retry_attempts=2,
        retry_backoff_seconds=0,
    )

    with pytest.raises(ExtractionTimeoutError):
        transport.complete_json(
            schema_name="test",
            schema={"type": "object"},
            system_prompt="system",
            user_prompt="user",
        )
    assert calls == 2


def test_invalid_structured_output_is_not_retried_by_transport() -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return _ollama_response("not-json", request)

    provider = LocalLLMProvider(
        base_url="http://ollama.test",
        model_name="qwen3.5:35b-a3b-q4_K_M",
        timeout_seconds=0.2,
        client=_transport(handler),
        retry_attempts=3,
        repair_attempts=0,
    )

    with pytest.raises(ExtractionInvalidJsonError):
        provider.extract("오늘 예측매니저 설치 가이드 수정했어.")
    assert calls == 1


def test_local_provider_final_timeout_does_not_write_canonical_memory(
    tmp_path: Path,
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("simulated timeout", request=request)

    provider = LocalLLMProvider(
        base_url="http://ollama.test",
        model_name="qwen3.5:35b-a3b-q4_K_M",
        timeout_seconds=0.2,
        client=_transport(handler),
        retry_attempts=2,
        retry_backoff_seconds=0,
        repair_attempts=0,
    )
    db_path = tmp_path / "timeout.sqlite"
    app = create_app(database_path=db_path, extractor=provider)
    with TestClient(app) as client:
        response = client.post(
            "/api/v1/chat/runs",
            json={
                "client_message_id": "local-timeout",
                "content": "오늘 예측매니저 설치 가이드 수정했어.",
            },
        )

    assert response.status_code == 503
    assert response.json()["error"]["code"] == "EXTRACTION_TIMEOUT"
    with sqlite3.connect(db_path) as connection:
        assert (
            connection.execute("SELECT COUNT(*) FROM projects").fetchone()[0]
            == 0
        )
        assert (
            connection.execute("SELECT COUNT(*) FROM work_items").fetchone()[0]
            == 0
        )
        assert (
            connection.execute("SELECT COUNT(*) FROM activities").fetchone()[0]
            == 0
        )


def test_local_provider_free_korean_capture_and_response_preserve_memory_boundary(
    tmp_path: Path,
) -> None:
    first = (
        "오늘 예측매니저 설치 문서 좀 손봤고 로그인 없앨지는 박사님한테 물어봤어."
    )
    second = "그때 물어본 로그인 건 답 왔는데 빼달래."
    outputs = [
        {
            "schema_version": "work-fact-draft.v1",
            "intent": "CAPTURE_WORK",
            "fact_groups": [
                {
                    "project_mention": "예측매니저",
                    "work_item_mention": "설치 가이드 및 로그인 기능 처리",
                    "activities": [
                        {
                            "kind": "WORK_PERFORMED",
                            "summary": "설치 문서 수정",
                            "occurred_on": "TODAY",
                            "source_excerpt": first,
                            "derivation": "EXPLICIT",
                            "rule_id": None,
                        },
                        {
                            "kind": "REQUEST_SENT",
                            "summary": "로그인 제거 여부 문의",
                            "occurred_on": "TODAY",
                            "source_excerpt": first,
                            "derivation": "EXPLICIT",
                            "rule_id": None,
                        },
                    ],
                    "proposed_patch": {
                        "status": "WAITING",
                        "priority": None,
                        "waiting_for": "로그인 제거 여부에 대한 박사님 회신",
                        "blocked_reason": None,
                        "next_action": None,
                        "clear_waiting_for": False,
                        "clear_blocked_reason": False,
                    },
                    "reference_terms": [],
                    "source_excerpt": first,
                }
            ],
            "query": None,
        },
        {
            "schema_version": "work-fact-draft.v1",
            "intent": "CAPTURE_WORK",
            "fact_groups": [
                {
                    "project_mention": None,
                    "work_item_mention": None,
                    "activities": [
                        {
                            "kind": "RESPONSE_RECEIVED",
                            "summary": "로그인 제거 회신 수신",
                            "occurred_on": "TODAY",
                            "source_excerpt": second,
                            "derivation": "EXPLICIT",
                            "rule_id": None,
                        }
                    ],
                    "proposed_patch": {
                        "status": "IN_PROGRESS",
                        "priority": None,
                        "waiting_for": None,
                        "blocked_reason": None,
                        "next_action": "로그인 기능 제거",
                        "clear_waiting_for": True,
                        "clear_blocked_reason": False,
                    },
                    "reference_terms": ["그때", "로그인"],
                    "source_excerpt": second,
                }
            ],
            "query": None,
        },
    ]
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        output = outputs[min(calls, len(outputs) - 1)]
        calls += 1
        return _ollama_response(json.dumps(output, ensure_ascii=False), request)

    provider = LocalLLMProvider(
        base_url="http://ollama.test",
        model_name="qwen3.5:35b-a3b-q4_K_M",
        timeout_seconds=0.2,
        client=_transport(handler),
        retry_attempts=1,
        repair_attempts=0,
    )
    db_path = tmp_path / "local-provider.sqlite"
    app = create_app(database_path=db_path, extractor=provider)
    with TestClient(app) as client:
        first_response = client.post(
            "/api/v1/chat/runs",
            json={"client_message_id": "local-1", "content": first},
        )
        second_response = client.post(
            "/api/v1/chat/runs",
            json={
                "client_message_id": "local-2",
                "conversation_id": first_response.json()["conversation_id"],
                "content": second,
            },
        )

    assert first_response.status_code == 200
    assert second_response.status_code == 200
    assert calls == 2
    with sqlite3.connect(db_path) as connection:
        project = connection.execute(
            "SELECT id, name FROM projects WHERE user_id='local-user'"
        ).fetchone()
        work_item = connection.execute(
            "SELECT id, status, waiting_for, next_action FROM work_items"
        ).fetchone()
        activities = connection.execute(
            "SELECT kind, summary FROM activities"
        ).fetchall()
    assert project and project[1] == "예측매니저"
    assert work_item and work_item[1:] == (
        "IN_PROGRESS",
        None,
        "로그인 기능 제거",
    )
    assert {row[0] for row in activities} == {
        "WORK_PERFORMED",
        "REQUEST_SENT",
        "RESPONSE_RECEIVED",
    }
    assert len(activities) == 3
