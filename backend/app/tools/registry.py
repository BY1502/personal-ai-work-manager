from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Callable

from app.database import Database


class Permission(StrEnum):
    ALLOW = "ALLOW"
    GUARDED = "GUARDED"
    CONFIRM = "CONFIRM"
    DENY = "DENY"
    JARVIS_ONLY = "JARVIS_ONLY"


class ToolExecutionError(RuntimeError):
    pass


class UnknownToolError(ToolExecutionError):
    pass


class ToolPermissionError(ToolExecutionError):
    pass


ToolHandler = Callable[[dict[str, Any], str], Any]


@dataclass(frozen=True)
class ToolDefinition:
    name: str
    permission: Permission
    description: str
    handler: ToolHandler


class ToolRegistry:
    def __init__(self) -> None:
        self._definitions: dict[str, ToolDefinition] = {}

    def register(self, definition: ToolDefinition) -> None:
        if definition.name in self._definitions:
            raise ToolExecutionError(f"duplicate tool: {definition.name}")
        self._definitions[definition.name] = definition

    def has(self, name: str) -> bool:
        return name in self._definitions

    def get(self, name: str) -> ToolDefinition | None:
        return self._definitions.get(name)

    def execute(
        self,
        *,
        name: str,
        payload: dict[str, Any],
        user_id: str,
        manifest_tools: set[str],
        manifest_permission: Permission,
        allow_guarded: bool = False,
    ) -> Any:
        definition = self._definitions.get(name)
        if definition is None:
            raise UnknownToolError(f"unknown tool: {name}")
        if name not in manifest_tools:
            raise ToolPermissionError("tool is not declared by the Skill manifest")
        if manifest_permission in {Permission.DENY, Permission.JARVIS_ONLY}:
            raise ToolPermissionError(f"manifest permission blocks tool: {name}")
        if definition.permission in {Permission.DENY, Permission.JARVIS_ONLY}:
            raise ToolPermissionError(f"runtime permission blocks tool: {name}")
        if definition.permission == Permission.CONFIRM and not allow_guarded:
            raise ToolPermissionError(f"tool requires user confirmation: {name}")
        if definition.permission == Permission.GUARDED and not allow_guarded:
            raise ToolPermissionError(f"guarded tool requires runtime policy: {name}")
        try:
            return definition.handler(payload, user_id)
        except ToolExecutionError:
            raise
        except Exception as exc:
            raise ToolExecutionError(f"tool execution failed: {name}") from exc


def build_default_tool_registry(
    *,
    database: Database,
    calendar_gateway: Any | None = None,
) -> ToolRegistry:
    """Register bounded read capabilities; no Worker receives a write Tool."""

    registry = ToolRegistry()

    def project_search(payload: dict[str, Any], user_id: str) -> list[dict[str, Any]]:
        term = str(payload.get("query", "")).strip()
        limit = _limit(payload.get("limit", 10))
        connection = database.connect()
        try:
            like = f"%{term}%"
            rows = connection.execute(
                """
                SELECT id AS project_id, name
                FROM projects
                WHERE user_id = ? AND archived_at IS NULL
                  AND (? = '' OR name LIKE ? OR normalized_name LIKE ?)
                ORDER BY updated_at DESC, id
                LIMIT ?
                """,
                (user_id, term, like, like, limit),
            ).fetchall()
            return [dict(row) for row in rows]
        finally:
            connection.close()

    def work_search(payload: dict[str, Any], user_id: str) -> list[dict[str, Any]]:
        term = str(payload.get("query", "")).strip()
        limit = _limit(payload.get("limit", 10))
        connection = database.connect()
        try:
            like = f"%{term}%"
            rows = connection.execute(
                """
                SELECT wi.id AS work_item_id, wi.title, wi.status,
                       wi.next_action, p.id AS project_id, p.name AS project_name
                FROM work_items wi
                JOIN projects p ON p.id = wi.project_id AND p.user_id = wi.user_id
                WHERE wi.user_id = ? AND wi.archived_at IS NULL
                  AND p.archived_at IS NULL
                  AND (? = '' OR wi.title LIKE ? OR wi.normalized_title LIKE ?
                       OR p.name LIKE ?)
                ORDER BY wi.updated_at DESC, wi.id
                LIMIT ?
                """,
                (user_id, term, like, like, like, limit),
            ).fetchall()
            return [dict(row) for row in rows]
        finally:
            connection.close()

    def recent_memory(payload: dict[str, Any], user_id: str) -> list[dict[str, Any]]:
        limit = _limit(payload.get("limit", 20), maximum=50)
        connection = database.connect()
        try:
            rows = connection.execute(
                """
                SELECT a.kind, a.summary, a.occurred_on_local,
                       p.name AS project_name, wi.title AS work_item_title
                FROM activities a
                JOIN activity_links al ON al.activity_id = a.id
                 AND al.user_id = a.user_id AND al.is_active = 1
                JOIN work_items wi ON wi.id = al.work_item_id
                 AND wi.user_id = al.user_id AND wi.archived_at IS NULL
                JOIN projects p ON p.id = wi.project_id
                 AND p.user_id = wi.user_id AND p.archived_at IS NULL
                WHERE a.user_id = ? AND a.validity = 'ACTIVE'
                ORDER BY a.recorded_at_utc DESC, a.id DESC
                LIMIT ?
                """,
                (user_id, limit),
            ).fetchall()
            return [dict(row) for row in rows]
        finally:
            connection.close()

    def calendar_events_list(payload: dict[str, Any], user_id: str) -> list[dict[str, Any]]:
        del user_id
        if calendar_gateway is None:
            raise ToolExecutionError("Google Calendar is not configured")
        time_min = str(payload.get("time_min", "")).strip()
        time_max = str(payload.get("time_max", "")).strip()
        if not time_min or not time_max:
            raise ToolExecutionError("calendar time_min and time_max are required")
        return calendar_gateway.list_events(
            time_min=time_min,
            time_max=time_max,
            limit=_limit(payload.get("limit", 20), maximum=50),
        )

    registry.register(
        ToolDefinition(
            name="project.search",
            permission=Permission.ALLOW,
            description="Search active projects by name or alias text.",
            handler=project_search,
        )
    )
    registry.register(
        ToolDefinition(
            name="work.search",
            permission=Permission.ALLOW,
            description="Search active Work Items by title or project.",
            handler=work_search,
        )
    )
    registry.register(
        ToolDefinition(
            name="memory.get_recent",
            permission=Permission.ALLOW,
            description="Read a bounded list of recent active Activities.",
            handler=recent_memory,
        )
    )
    registry.register(
        ToolDefinition(
            name="calendar.events.list",
            permission=Permission.ALLOW,
            description="Read a bounded Google Calendar event range.",
            handler=calendar_events_list,
        )
    )
    return registry


def _limit(value: Any, *, maximum: int = 20) -> int:
    try:
        value = int(value)
    except (TypeError, ValueError):
        value = 10
    return max(1, min(maximum, value))
