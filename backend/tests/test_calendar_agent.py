from __future__ import annotations

import sqlite3
from pathlib import Path

from fastapi.testclient import TestClient

from app.calendar_agent import CalendarProviderTimeout, UnconfiguredCalendarGateway
from app.extraction import DeterministicTestProvider
from app.main import create_app


class FakeCalendarGateway:
    provider_name = "google-calendar-test"
    configured = True

    def __init__(self) -> None:
        self.list_calls: list[dict] = []
        self.create_calls: list[dict] = []
        self.events: dict[str, dict] = {}

    def list_events(self, *, time_min: str, time_max: str, limit: int):
        self.list_calls.append(
            {"time_min": time_min, "time_max": time_max, "limit": limit}
        )
        return [
            {
                "id": "provider-event-1",
                "summary": "박사님 미팅",
                "start": {"dateTime": "2026-08-21T14:00:00+09:00"},
                "end": {"dateTime": "2026-08-21T15:00:00+09:00"},
            }
        ]

    def get_event(self, event_id: str):
        return self.events.get(event_id)

    def create_event(self, *, event_id: str, payload: dict):
        self.create_calls.append({"event_id": event_id, "payload": payload})
        event = {"id": event_id, **payload}
        self.events[event_id] = event
        return event


class CalendarWorker(DeterministicTestProvider):
    def execute_skill(
        self,
        *,
        skill_name,
        model_profile,
        input_payload,
        context,
        output_schema,
    ):
        assert skill_name == "calendar-agent"
        assert model_profile == "worker-balanced"
        assert output_schema["properties"]["action"]["enum"] == ["LIST", "CREATE"]
        assert context["context_package"]["allowed_tools"] == [
            "calendar.events.list"
        ]
        content = input_payload["content"]
        if "알려" in content:
            return {
                "schema_version": "calendar-action-draft.v1",
                "action": "LIST",
                "title": None,
                "start_at": None,
                "end_at": None,
                "date_from": "2026-08-21",
                "date_to": "2026-08-21",
                "timezone": "Asia/Seoul",
            }
        return {
            "schema_version": "calendar-action-draft.v1",
            "action": "CREATE",
            "title": "박사님 미팅",
            "start_at": "2026-08-21T14:00:00+09:00",
            "end_at": "2026-08-21T15:00:00+09:00",
            # Real structured-output models may redundantly return these.
            # They are claim-free for CREATE and deterministic code ignores them.
            "date_from": "2026-08-21",
            "date_to": "2026-08-21",
            "timezone": "Asia/Seoul",
        }


class InvalidCalendarWorker(CalendarWorker):
    def execute_skill(self, **kwargs):
        del kwargs
        return {
            "schema_version": "calendar-action-draft.v1",
            "action": "CREATE",
            "title": "잘못된 일정",
        }


class AmbiguousWriteGateway(FakeCalendarGateway):
    def __init__(self) -> None:
        super().__init__()
        self.get_calls = 0

    def get_event(self, event_id: str):
        del event_id
        self.get_calls += 1
        return None

    def create_event(self, *, event_id: str, payload: dict):
        self.create_calls.append({"event_id": event_id, "payload": payload})
        raise CalendarProviderTimeout("timeout after possible provider write")


def _app(
    tmp_path: Path,
    monkeypatch,
    *,
    worker=None,
    gateway=None,
):
    monkeypatch.setenv("TTS_ENABLED", "false")
    monkeypatch.setenv("SKILL_AUTO_ENABLE", "work-capture,calendar-agent")
    return create_app(
        database_path=tmp_path / "calendar.sqlite",
        extractor=worker or CalendarWorker(),
        calendar_gateway=gateway or FakeCalendarGateway(),
    )


def _chat(client: TestClient, message_id: str, content: str):
    return client.post(
        "/api/v1/chat/runs",
        json={"client_message_id": message_id, "content": content},
    )


def _canonical_counts(path: Path) -> tuple[int, int, int]:
    connection = sqlite3.connect(path)
    try:
        return tuple(
            connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            for table in ("projects", "work_items", "activities")
        )
    finally:
        connection.close()


def test_calendar_list_uses_declared_read_tool_only(tmp_path: Path, monkeypatch) -> None:
    gateway = FakeCalendarGateway()
    app = _app(tmp_path, monkeypatch, gateway=gateway)
    with TestClient(app) as client:
        response = _chat(client, "calendar-list-1", "내일 일정 알려줘.")

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "COMPLETED"
    assert "박사님 미팅" in body["display_response"]
    assert body["data"]["calendar"]["kind"] == "EVENT_LIST"
    assert len(gateway.list_calls) == 1
    assert gateway.create_calls == []
    assert _canonical_counts(tmp_path / "calendar.sqlite") == (0, 0, 0)

    connection = sqlite3.connect(tmp_path / "calendar.sqlite")
    try:
        event_types = {
            row[0]
            for row in connection.execute(
                "SELECT event_type FROM execution_events"
            ).fetchall()
        }
    finally:
        connection.close()
    assert "TOOL_PERMISSION_DECIDED" in event_types


def test_calendar_create_requires_approval_and_duplicate_click_writes_once(
    tmp_path: Path,
    monkeypatch,
) -> None:
    gateway = FakeCalendarGateway()
    app = _app(tmp_path, monkeypatch, gateway=gateway)
    with TestClient(app) as client:
        proposed = _chat(
            client,
            "calendar-create-1",
            "내일 오후 2시에 박사님 미팅 1시간 등록해줘.",
        )
        assert proposed.status_code == 200
        proposal = proposed.json()["data"]["calendar"]["proposal"]
        assert proposal["status"] == "PENDING_APPROVAL"
        assert proposal["requires_approval"] is True
        assert gateway.create_calls == []

        headers = {"Idempotency-Key": "calendar-approve-stable-1"}
        payload = {"action": "APPROVE", "expected_version": proposal["version"]}
        approved = client.post(
            f"/api/v1/calendar/proposals/{proposal['proposal_id']}/resolve",
            json=payload,
            headers=headers,
        )
        replay = client.post(
            f"/api/v1/calendar/proposals/{proposal['proposal_id']}/resolve",
            json=payload,
            headers=headers,
        )

    assert approved.status_code == 200
    assert replay.status_code == 200
    assert approved.json()["status"] == "COMPLETED"
    assert replay.json()["status"] == "COMPLETED"
    assert len(gateway.create_calls) == 1
    assert _canonical_counts(tmp_path / "calendar.sqlite") == (0, 0, 0)


def test_calendar_reject_never_calls_google_write(tmp_path: Path, monkeypatch) -> None:
    gateway = FakeCalendarGateway()
    app = _app(tmp_path, monkeypatch, gateway=gateway)
    with TestClient(app) as client:
        proposed = _chat(
            client,
            "calendar-reject-1",
            "내일 오후 2시에 박사님 미팅 1시간 등록해줘.",
        ).json()["data"]["calendar"]["proposal"]
        rejected = client.post(
            f"/api/v1/calendar/proposals/{proposed['proposal_id']}/resolve",
            json={"action": "REJECT", "expected_version": proposed["version"]},
            headers={"Idempotency-Key": "calendar-reject-stable-1"},
        )

    assert rejected.status_code == 200
    assert rejected.json()["status"] == "REJECTED"
    assert gateway.create_calls == []


def test_invalid_calendar_output_does_not_change_memory_or_create_proposal(
    tmp_path: Path,
    monkeypatch,
) -> None:
    app = _app(tmp_path, monkeypatch, worker=InvalidCalendarWorker())
    with TestClient(app) as client:
        response = _chat(
            client,
            "calendar-invalid-1",
            "내일 오후 2시에 잘못된 일정 등록해줘.",
        )

    assert response.status_code == 503
    assert response.json()["error"]["code"] in {
        "SKILL_OUTPUT_INVALID",
        "SKILL_ITERATION_LIMIT",
    }
    assert _canonical_counts(tmp_path / "calendar.sqlite") == (0, 0, 0)
    connection = sqlite3.connect(tmp_path / "calendar.sqlite")
    try:
        assert connection.execute(
            "SELECT COUNT(*) FROM calendar_action_proposals"
        ).fetchone()[0] == 0
    finally:
        connection.close()


def test_unconfigured_calendar_status_does_not_expose_credentials(
    tmp_path: Path,
    monkeypatch,
) -> None:
    app = _app(
        tmp_path,
        monkeypatch,
        gateway=UnconfiguredCalendarGateway(),
    )
    with TestClient(app) as client:
        status = client.get("/api/v1/calendar/status")

    assert status.status_code == 200
    assert status.json() == {
        "provider": "google-calendar",
        "state": "UNCONFIGURED",
        "message": "Google Calendar 연결 필요",
    }
    serialized = status.text.lower()
    assert "secret" not in serialized
    assert "token" not in serialized


def test_ambiguous_google_write_is_not_repeated_on_approval_replay(
    tmp_path: Path,
    monkeypatch,
) -> None:
    gateway = AmbiguousWriteGateway()
    app = _app(tmp_path, monkeypatch, gateway=gateway)
    with TestClient(app) as client:
        proposal = _chat(
            client,
            "calendar-ambiguous-1",
            "내일 오후 2시에 박사님 미팅 1시간 등록해줘.",
        ).json()["data"]["calendar"]["proposal"]
        endpoint = (
            f"/api/v1/calendar/proposals/{proposal['proposal_id']}/resolve"
        )
        body = {"action": "APPROVE", "expected_version": proposal["version"]}
        headers = {"Idempotency-Key": "calendar-ambiguous-stable-1"}
        first = client.post(endpoint, json=body, headers=headers)
        replay = client.post(endpoint, json=body, headers=headers)

    assert first.status_code == 409
    assert replay.status_code == 409
    assert first.json()["error"]["code"] == "CALENDAR_EXECUTION_UNCERTAIN"
    assert replay.json()["error"]["code"] == "CALENDAR_EXECUTION_UNCERTAIN"
    assert len(gateway.create_calls) == 1
    assert gateway.get_calls == 1

    connection = sqlite3.connect(tmp_path / "calendar.sqlite")
    try:
        status = connection.execute(
            "SELECT status FROM calendar_action_proposals"
        ).fetchone()[0]
    finally:
        connection.close()
    assert status == "UNKNOWN"
