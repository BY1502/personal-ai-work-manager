from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.database import Database
from app.extraction import DeterministicTestProvider
from app.main import create_app
from app.providers import ExtractionTimeoutError
from app.skills.registry import SkillRegistry
from app.tools.registry import (
    Permission,
    ToolPermissionError,
    ToolRegistry,
    UnknownToolError,
    build_default_tool_registry,
)
from app.utils import SystemClock


class InvalidProvider:
    provider_name = "test-invalid"
    version = "test-invalid-v1"
    model_name = "invalid"
    prompt_version = "test"

    def extract(self, content: str):
        del content
        return {"schema_version": "work-fact-draft.v1", "intent": "CAPTURE_WORK"}


class TimeoutProvider:
    provider_name = "test-timeout"
    version = "test-timeout-v1"
    model_name = "timeout"
    prompt_version = "test"

    def extract(self, content: str):
        del content
        raise ExtractionTimeoutError("test timeout")


class CapturingProvider(DeterministicTestProvider):
    def __init__(self) -> None:
        self.context = None

    def extract_with_context(self, content: str, context: dict):
        self.context = context
        return self.extract(content)


class RetryAfterValidationProvider(DeterministicTestProvider):
    """First worker result is schema-valid but rejected by the domain gate."""

    def __init__(self) -> None:
        self.calls = 0

    def extract_with_context(self, content: str, context: dict):
        del context
        payload = self.extract(content).model_dump(mode="json")
        self.calls += 1
        if self.calls == 1:
            payload["fact_groups"][0]["activities"][0]["derivation"] = (
                "LLM_INFERRED"
            )
        return payload


def _chat(client: TestClient, content: str, message_id: str = "m1"):
    return client.post(
        "/api/v1/chat/runs",
        json={"client_message_id": message_id, "content": content},
    )


def _counts(path: Path) -> dict[str, int]:
    connection = sqlite3.connect(path)
    try:
        return {
            table: connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            for table in ("projects", "work_items", "activities", "change_audit")
        }
    finally:
        connection.close()


def test_work_capture_vertical_slice_uses_registry_runtime_and_phase1_apply(tmp_path: Path) -> None:
    db_path = tmp_path / "slice.sqlite"
    app = create_app(database_path=db_path, extractor=DeterministicTestProvider())
    with TestClient(app) as client:
        skills = client.get("/api/v1/skills")
        assert skills.status_code == 200
        assert skills.json()["items"][0]["state"] == "ENABLED"
        response = _chat(
            client,
            "오늘 예측매니저 설치 가이드 수정했고 로그인 제거 여부는 확인 요청해놨어.",
        )
        assert response.status_code == 200
        body = response.json()
        assert body["status"] == "COMPLETED"
        assert "회신 대기" in body["display_response"]

    connection = sqlite3.connect(db_path)
    try:
        row = connection.execute(
            "SELECT state, skill_name, iteration, output_json FROM skill_executions"
        ).fetchone()
        assert row[:3] == ("COMPLETED", "work-capture", 1)
        assert row[3]
        events = {
            value[0]
            for value in connection.execute(
                "SELECT event_type FROM execution_events"
            ).fetchall()
        }
        assert {"SKILL_SELECTED", "SKILL_LOADED", "SKILL_COMPLETED"} <= events
        assert connection.execute("SELECT COUNT(*) FROM projects").fetchone()[0] == 1
        assert connection.execute("SELECT COUNT(*) FROM work_items").fetchone()[0] == 1
        assert connection.execute("SELECT COUNT(*) FROM activities").fetchone()[0] == 2
    finally:
        connection.close()


def test_legacy_chat_payload_aliases_and_ui_metadata_are_tolerated(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "legacy-chat.sqlite"
    app = create_app(database_path=db_path, extractor=DeterministicTestProvider())
    with TestClient(app) as client:
        response = client.post(
            "/api/v1/chat/runs",
            json={
                "text": "오늘 예측매니저 설치 가이드 수정했어.",
                "ui_source": "cached-dashboard",
                "conversationId": None,
            },
        )
        assert response.status_code == 200
        assert response.json()["status"] == "COMPLETED"


def test_chat_accepts_plain_json_text_from_desktop_shell(tmp_path: Path) -> None:
    """Older Spotlight shells sometimes POST the text itself, not an object."""
    app = create_app(
        database_path=tmp_path / "plain-chat.sqlite",
        extractor=DeterministicTestProvider(),
    )
    with TestClient(app) as client:
        response = client.post(
            "/api/v1/chat/runs",
            content='"오늘 예측매니저 설치 가이드 수정했어."',
            headers={"Content-Type": "application/json"},
        )
        assert response.status_code == 200
        assert response.json()["status"] == "COMPLETED"


def test_runtime_loads_skill_body_and_bounded_context_package(tmp_path: Path) -> None:
    provider = CapturingProvider()
    app = create_app(database_path=tmp_path / "context.sqlite", extractor=provider)
    with TestClient(app) as client:
        response = _chat(client, "오늘 예측매니저 설치 가이드 수정했어.")
        assert response.status_code == 200
    assert provider.context is not None
    assert provider.context["skill"]["name"] == "work-capture"
    assert "# Work Capture" in provider.context["skill"]["instructions"]
    assert "context_package" in provider.context
    assert "content" not in provider.context["context_package"]["request"]


def test_failed_validation_replay_refreshes_completed_skill_output(
    tmp_path: Path,
) -> None:
    provider = RetryAfterValidationProvider()
    db_path = tmp_path / "retry-after-validation.sqlite"
    app = create_app(database_path=db_path, extractor=provider)
    with TestClient(app) as client:
        first = _chat(client, "오늘 예측매니저 설치 가이드 수정했어.", "retry-1")
        assert first.status_code == 422
        assert first.json()["error"]["code"] == "DETERMINISTIC_VALIDATION_FAILED"
        second = _chat(client, "오늘 예측매니저 설치 가이드 수정했어.", "retry-1")
        assert second.status_code == 200
        assert second.json()["status"] == "COMPLETED"

    assert provider.calls == 2
    assert _counts(db_path) == {
        "projects": 1,
        "work_items": 1,
        "activities": 1,
        "change_audit": 3,
    }


def test_disabled_skill_does_not_execute_or_write(tmp_path: Path) -> None:
    db_path = tmp_path / "disabled.sqlite"
    app = create_app(database_path=db_path, extractor=DeterministicTestProvider())
    with TestClient(app) as client:
        app.state.skill_registry.set_state("work-capture", "DISABLED")
        response = _chat(client, "오늘 예측매니저 설치 가이드 수정했어.")
        assert response.status_code == 503
        assert response.json()["error"]["code"] == "SKILL_RUNTIME_FAILED"
    assert _counts(db_path) == {
        "projects": 0,
        "work_items": 0,
        "activities": 0,
        "change_audit": 0,
    }


def test_invalid_output_does_not_write_canonical_memory(tmp_path: Path) -> None:
    db_path = tmp_path / "invalid.sqlite"
    app = create_app(database_path=db_path, extractor=InvalidProvider())
    with TestClient(app) as client:
        response = _chat(client, "오늘 예측매니저 설치 가이드 수정했어.")
        assert response.status_code == 503
        assert response.json()["error"]["code"] == "SKILL_OUTPUT_INVALID"
    assert _counts(db_path) == {
        "projects": 0,
        "work_items": 0,
        "activities": 0,
        "change_audit": 0,
    }


def test_provider_timeout_does_not_write_canonical_memory(tmp_path: Path) -> None:
    db_path = tmp_path / "timeout.sqlite"
    app = create_app(database_path=db_path, extractor=TimeoutProvider())
    with TestClient(app) as client:
        response = _chat(client, "오늘 예측매니저 설치 가이드 수정했어.")
        assert response.status_code == 503
        assert response.json()["error"]["code"] == "EXTRACTION_TIMEOUT"
    assert _counts(db_path) == {
        "projects": 0,
        "work_items": 0,
        "activities": 0,
        "change_audit": 0,
    }


def test_manifest_unknown_tool_is_rejected(tmp_path: Path) -> None:
    db_path = tmp_path / "registry.sqlite"
    database = Database(db_path, clock=SystemClock())
    database.initialize()
    tools = build_default_tool_registry(database=database)
    root = tmp_path / "skills"
    skill_dir = root / "unknown"
    (skill_dir / "schemas").mkdir(parents=True)
    (skill_dir / "schemas" / "input.json").write_text('{"type":"object"}')
    (skill_dir / "schemas" / "output.json").write_text('{"type":"object"}')
    (skill_dir / "SKILL.md").write_text(
        """---
schema_version: skill-manifest.v1
name: unknown-tool
version: 1.0.0
description: invalid
model_profile: worker-balanced
tools: [not.registered]
permissions:
  not.registered: ALLOW
input_schema: schemas/input.json
output_schema: schemas/output.json
---
body
""",
        encoding="utf-8",
    )
    registry = SkillRegistry(
        root=root,
        database=database,
        tool_registry=tools,
        auto_enable_names={"unknown-tool"},
    )
    registry.refresh()
    assert "unknown tool" in next(iter(registry.errors.values()))
    assert registry.get("unknown-tool") is None


def test_manifest_invalid_yaml_and_schema_reference_are_rejected(tmp_path: Path) -> None:
    db_path = tmp_path / "invalid-registry.sqlite"
    database = Database(db_path, clock=SystemClock())
    database.initialize()
    tools = build_default_tool_registry(database=database)
    root = tmp_path / "skills"
    invalid_yaml = root / "invalid-yaml"
    invalid_yaml.mkdir(parents=True)
    (invalid_yaml / "SKILL.md").write_text(
        "---\nname: [broken\n---\nbody\n", encoding="utf-8"
    )
    invalid_schema = root / "invalid-schema"
    invalid_schema.mkdir(parents=True)
    (invalid_schema / "SKILL.md").write_text(
        """---
schema_version: skill-manifest.v1
name: invalid-schema
version: 1.0.0
description: invalid schema path
model_profile: worker-balanced
tools: []
permissions: {}
input_schema: schemas/missing.json
output_schema: schemas/missing.json
---
body
""",
        encoding="utf-8",
    )
    registry = SkillRegistry(
        root=root,
        database=database,
        tool_registry=tools,
        auto_enable_names={"invalid-yaml", "invalid-schema"},
    )
    registry.refresh()
    assert len(registry.errors) == 2
    assert registry.get("invalid-yaml") is None
    assert registry.get("invalid-schema") is None


def test_tool_registry_permission_and_unknown_tool_guards(tmp_path: Path) -> None:
    database = Database(tmp_path / "tools.sqlite", clock=SystemClock())
    database.initialize()
    registry = build_default_tool_registry(database=database)
    assert registry.execute(
        name="project.search",
        payload={"query": "예측"},
        user_id="local-user",
        manifest_tools={"project.search"},
        manifest_permission=Permission.ALLOW,
    ) == []
    with pytest.raises(ToolPermissionError):
        registry.execute(
            name="project.search",
            payload={},
            user_id="local-user",
            manifest_tools={"project.search"},
            manifest_permission=Permission.DENY,
        )
    with pytest.raises(UnknownToolError):
        registry.execute(
            name="missing.tool",
            payload={},
            user_id="local-user",
            manifest_tools={"missing.tool"},
            manifest_permission=Permission.ALLOW,
        )


def test_iteration_budget_stops_invalid_worker_without_write(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = tmp_path / "skills"
    skill_dir = root / "work-capture"
    (skill_dir / "schemas").mkdir(parents=True)
    (skill_dir / "schemas" / "input.json").write_text('{"type":"object"}')
    (skill_dir / "schemas" / "output.json").write_text('{"type":"object"}')
    (skill_dir / "SKILL.md").write_text(
        """---
schema_version: skill-manifest.v1
name: work-capture
version: 1.0.0
description: invalid output budget test
model_profile: worker-balanced
max_iterations: 1
tools: []
permissions: {}
input_schema: schemas/input.json
output_schema: schemas/output.json
failure_policy:
  retry: 3
---
body
""",
        encoding="utf-8",
    )
    monkeypatch.setenv("SKILLS_ROOT", str(root))
    db_path = tmp_path / "budget.sqlite"
    app = create_app(database_path=db_path, extractor=InvalidProvider())
    with TestClient(app) as client:
        response = _chat(client, "오늘 예측매니저 설치 가이드 수정했어.")
        assert response.status_code == 503
        assert response.json()["error"]["code"] == "SKILL_ITERATION_LIMIT"
    connection = sqlite3.connect(db_path)
    try:
        row = connection.execute(
            "SELECT state, iteration FROM skill_executions"
        ).fetchone()
        assert row == ("FAILED", 1)
    finally:
        connection.close()
