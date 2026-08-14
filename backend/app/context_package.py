from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta
from typing import Any

from app.database import Database
from app.utils import local_date, sha256_text
from app.work_queries import StructuredWorkQueryService


@dataclass(frozen=True)
class ContextPackage:
    payload: dict[str, Any]
    digest: str


class ContextPackageBuilder:
    """Builds a bounded, read-only context for a Skill Worker."""

    def __init__(self, *, database: Database, work_queries: StructuredWorkQueryService) -> None:
        self.database = database
        self.work_queries = work_queries

    def build(
        self,
        *,
        user_id: str,
        conversation_id: str,
        content: str,
        recent_days: int = 30,
    ) -> ContextPackage:
        today = local_date(self.database.clock, self.database.timezone_name)
        current = self.work_queries.list_by_status(
            user_id=user_id,
            statuses=("IN_PROGRESS", "WAITING", "BLOCKED"),
            limit=8,
        )
        focus = self.work_queries.focused_work_item(
            user_id=user_id,
            conversation_id=conversation_id,
        )
        start = today - timedelta(days=max(0, min(365, recent_days)))
        recent = self.work_queries.activities_between(
            user_id=user_id,
            start_date=start,
            end_date=today,
            limit=30,
        )
        payload = {
            "request": {"content_digest": sha256_text(content)},
            "current_focus": _safe_focus(focus),
            "current_work": [_safe_work(item) for item in current],
            "recent_activities": [_safe_activity(item) for item in recent],
            "constraints": {
                "today_local": today.isoformat(),
                "timezone": self.database.timezone_name,
                "do_not_write": True,
            },
            "allowed_actions": ["RETURN_WORK_FACT_DRAFT"],
            "allowed_tools": ["project.search", "work.search", "memory.get_recent"],
        }
        return ContextPackage(payload=payload, digest=sha256_text(_stable_json(payload)))


def _safe_focus(value: dict | None) -> dict | None:
    if not value:
        return None
    return {
        key: value.get(key)
        for key in ("project_name", "title", "status", "next_action", "waiting_for")
        if value.get(key) is not None
    }


def _safe_work(value: dict) -> dict:
    return {
        key: value.get(key)
        for key in (
            "project_name",
            "title",
            "status",
            "priority",
            "waiting_for",
            "blocked_reason",
            "next_action",
            "last_activity_on",
        )
        if value.get(key) is not None
    }


def _safe_activity(value: dict) -> dict:
    return {
        key: value.get(key)
        for key in ("project_name", "work_item_title", "kind", "summary", "occurred_on_local")
        if value.get(key) is not None
    }


def _stable_json(value: object) -> str:
    import json

    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
