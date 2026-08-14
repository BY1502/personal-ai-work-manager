from __future__ import annotations

import json
import sqlite3
import shutil
from datetime import datetime, timezone
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.database import Database
from app.event_engine import DomainEvent, EventEngine
from app.extraction import DeterministicTestProvider
from app.main import create_app
from app.permissions import PermissionEngine
from app.tools.registry import Permission, build_default_tool_registry
from app.utils import FrozenClock, new_id, utc_iso


def _seed_waiting(database: Database, *, days_old: int = 5) -> None:
    now = database.clock.now_utc()
    today = now.astimezone(timezone.utc).date()
    project_id = new_id("proj")
    work_id = new_id("work")
    old_date = today.fromordinal(today.toordinal() - days_old).isoformat()
    stamp = utc_iso(now)
    with database.transaction() as connection:
        connection.execute(
            """
            INSERT INTO projects(
                id,user_id,name,normalized_name,created_at,updated_at
            ) VALUES (?, 'local-user', '예측매니저', '예측매니저', ?, ?)
            """,
            (project_id, stamp, stamp),
        )
        connection.execute(
            """
            INSERT INTO work_items(
                id,user_id,project_id,title,normalized_title,status,priority,
                waiting_for,status_changed_at,last_activity_on,created_at,updated_at
            ) VALUES (?, 'local-user', ?, '로그인 제거 문의', '로그인제거문의',
                      'WAITING', 'NORMAL', '담당자 회신', ?, ?, ?, ?)
            """,
            (work_id, project_id, stamp, old_date, stamp, stamp),
        )


def test_event_engine_deduplicates_events_and_generates_bounded_trigger(tmp_path: Path) -> None:
    clock = FrozenClock(datetime(2026, 8, 14, 1, 0, tzinfo=timezone.utc))
    database = Database(tmp_path / "triggers.sqlite", clock=clock)
    database.initialize()
    _seed_waiting(database)
    engine = EventEngine(database)
    event = DomainEvent(
        event_type="MEMORY_CHANGED",
        aggregate_type="WORK_ITEM",
        aggregate_id="work-1",
        payload={"memory_status": "APPLIED"},
    )
    assert engine.emit(user_id="local-user", event=event) is True
    assert engine.emit(user_id="local-user", event=event) is False
    suggestions = engine.suggestions(user_id="local-user")
    assert len(suggestions) == 1
    assert suggestions[0].trigger_type == "WAITING_TOO_LONG"
    assert "회신" in suggestions[0].title
    connection = sqlite3.connect(database.path)
    try:
        assert connection.execute("SELECT COUNT(*) FROM domain_events").fetchone()[0] == 1
        assert connection.execute("SELECT COUNT(*) FROM trigger_suggestions").fetchone()[0] == 1
    finally:
        connection.close()


def test_trigger_suggestions_do_not_write_canonical_memory(tmp_path: Path) -> None:
    clock = FrozenClock(datetime(2026, 8, 14, 1, 0, tzinfo=timezone.utc))
    database = Database(tmp_path / "readonly.sqlite", clock=clock)
    database.initialize()
    _seed_waiting(database)
    before = {}
    connection = sqlite3.connect(database.path)
    try:
        for table in ("projects", "work_items", "activities", "change_audit"):
            before[table] = connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
    finally:
        connection.close()
    EventEngine(database).suggestions(user_id="local-user")
    connection = sqlite3.connect(database.path)
    try:
        after = {
            table: connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            for table in before
        }
    finally:
        connection.close()
    assert after == before


def test_trigger_suggestion_expires_after_condition_is_resolved(tmp_path: Path) -> None:
    clock = FrozenClock(datetime(2026, 8, 14, 1, 0, tzinfo=timezone.utc))
    database = Database(tmp_path / "expiry.sqlite", clock=clock)
    database.initialize()
    _seed_waiting(database)
    engine = EventEngine(database)
    engine.emit(
        user_id="local-user",
        event=DomainEvent(
            event_type="MEMORY_CHANGED",
            aggregate_type="WORK_ITEM",
            aggregate_id="work-1",
            payload={"memory_status": "APPLIED"},
        ),
    )
    assert len(engine.suggestions(user_id="local-user")) == 1
    with database.transaction() as connection:
        connection.execute(
            "UPDATE work_items SET status = 'IN_PROGRESS', waiting_for = NULL, last_activity_on = ?",
            (clock.now_utc().astimezone(timezone.utc).date().isoformat(),),
        )
    assert engine.suggestions(user_id="local-user") == []
    connection = sqlite3.connect(database.path)
    try:
        assert connection.execute(
            "SELECT status FROM trigger_suggestions"
        ).fetchone()[0] == "EXPIRED"
    finally:
        connection.close()


def test_permission_engine_allows_declared_read_and_blocks_other_requests(tmp_path: Path) -> None:
    database = Database(tmp_path / "permissions.sqlite", clock=FrozenClock(
        datetime(2026, 8, 14, 1, 0, tzinfo=timezone.utc)
    ))
    database.initialize()
    engine = PermissionEngine(build_default_tool_registry(database=database))
    allowed = engine.evaluate(
        tool_name="project.search",
        manifest_tools={"project.search"},
        manifest_permission=Permission.ALLOW,
    )
    assert allowed.allowed is True
    assert allowed.reason_code == "ALLOWED"
    missing = engine.evaluate(
        tool_name="work.search",
        manifest_tools={"project.search"},
        manifest_permission=Permission.ALLOW,
    )
    assert missing.allowed is False
    assert missing.reason_code == "TOOL_NOT_IN_MANIFEST"
    unknown = engine.evaluate(
        tool_name="filesystem.write",
        manifest_tools={"filesystem.write"},
        manifest_permission=Permission.ALLOW,
    )
    assert unknown.allowed is False
    assert unknown.reason_code == "UNKNOWN_TOOL"
    denied = engine.evaluate(
        tool_name="project.search",
        manifest_tools={"project.search"},
        manifest_permission=Permission.DENY,
    )
    assert denied.allowed is False
    assert denied.reason_code == "MANIFEST_PERMISSION_BLOCKED"


class GenericWorker(DeterministicTestProvider):
    def execute_skill(self, *, skill_name, model_profile, input_payload, context):
        assert skill_name == "echo"
        assert model_profile == "worker-balanced"
        assert context["skill"]["name"] == "echo"
        return {"ok": True, "echo": input_payload["value"]}


class InvalidGenericWorker(GenericWorker):
    def execute_skill(self, **kwargs):
        return {"wrong": True}


def _write_echo_skill(root: Path) -> None:
    shutil.copytree(Path(__file__).parents[1] / "skills" / "work-capture", root / "work-capture")
    skill_dir = root / "echo"
    (skill_dir / "schemas").mkdir(parents=True)
    (skill_dir / "schemas" / "input.json").write_text(
        json.dumps(
            {
                "type": "object",
                "additionalProperties": False,
                "required": ["value"],
                "properties": {"value": {"type": "string", "minLength": 1}},
            }
        ),
        encoding="utf-8",
    )
    (skill_dir / "schemas" / "output.json").write_text(
        json.dumps(
            {
                "type": "object",
                "additionalProperties": False,
                "required": ["ok", "echo"],
                "properties": {
                    "ok": {"const": True},
                    "echo": {"type": "string"},
                },
            }
        ),
        encoding="utf-8",
    )
    (skill_dir / "SKILL.md").write_text(
        """---
schema_version: skill-manifest.v1
name: echo
version: 1.0.0
description: generic test skill
model_profile: worker-balanced
max_iterations: 2
timeout_seconds: 5
tools: []
permissions: {}
memory_scope:
  recent_days: 7
  scope: none
input_schema: schemas/input.json
output_schema: schemas/output.json
failure_policy:
  retry: 0
---
# Echo
""",
        encoding="utf-8",
    )


def test_generic_skill_runtime_executes_declared_contract(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = tmp_path / "skills"
    _write_echo_skill(root)
    monkeypatch.setenv("SKILLS_ROOT", str(root))
    monkeypatch.setenv("SKILL_AUTO_ENABLE", "work-capture,echo")
    app = create_app(
        database_path=tmp_path / "generic.sqlite",
        extractor=GenericWorker(),
    )
    with TestClient(app) as client:
        chat = client.post(
            "/api/v1/chat/runs",
            json={
                "client_message_id": "generic-seed",
                "content": "오늘 예측매니저 설치 가이드 수정했어.",
            },
        )
        assert chat.status_code == 200
        result = app.state.skill_runtime.invoke(
            user_id="local-user",
            run_id=chat.json()["run_id"],
            conversation_id=chat.json()["conversation_id"],
            skill_name="echo",
            input_payload={"value": "hello"},
        )
    assert result.output == {"echo": "hello", "ok": True}
    assert result.envelope is None


def test_generic_skill_invalid_output_never_reaches_canonical_memory(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = tmp_path / "skills"
    _write_echo_skill(root)
    monkeypatch.setenv("SKILLS_ROOT", str(root))
    monkeypatch.setenv("SKILL_AUTO_ENABLE", "work-capture,echo")
    app = create_app(
        database_path=tmp_path / "generic-invalid.sqlite",
        extractor=InvalidGenericWorker(),
    )
    with TestClient(app) as client:
        chat = client.post(
            "/api/v1/chat/runs",
            json={
                "client_message_id": "generic-seed",
                "content": "오늘 예측매니저 설치 가이드 수정했어.",
            },
        )
        assert chat.status_code == 200
        with pytest.raises(Exception) as error:
            app.state.skill_runtime.invoke(
                user_id="local-user",
                run_id=chat.json()["run_id"],
                conversation_id=chat.json()["conversation_id"],
                skill_name="echo",
                input_payload={"value": "hello"},
            )
        assert getattr(error.value, "code", None) == "SKILL_OUTPUT_INVALID"
    connection = sqlite3.connect(tmp_path / "generic-invalid.sqlite")
    try:
        assert connection.execute("SELECT COUNT(*) FROM projects").fetchone()[0] == 1
        assert connection.execute("SELECT COUNT(*) FROM activities").fetchone()[0] == 1
    finally:
        connection.close()
