from __future__ import annotations

from typing import Any

import httpx

from app.database import Database
from app.extraction import ExtractionProvider
from app.recommendation import RecommendationService
from app.repository import ResourceNotFound
from app.utils import local_date, utc_iso
from app.work_queries import StructuredWorkQueryService


class DashboardReadService:
    """Small, read-only projections for the personal dashboard.

    The browser is never treated as canonical state. Every projection is rebuilt
    from active Structured Work Memory rows on each request.
    """

    def __init__(
        self,
        *,
        database: Database,
        work_queries: StructuredWorkQueryService,
        recommendations: RecommendationService,
        extractor: ExtractionProvider,
    ) -> None:
        self.database = database
        self.work_queries = work_queries
        self.recommendations = recommendations
        self.extractor = extractor

    def summary(self, *, user_id: str) -> dict[str, list[dict[str, Any]]]:
        today = local_date(self.database.clock, self.database.timezone_name)
        return {
            "current_work": [
                self._work_item(item)
                for item in self.work_queries.list_by_status(
                    user_id=user_id,
                    statuses=("IN_PROGRESS",),
                    limit=8,
                )
            ],
            "waiting": [
                self._work_item(item)
                for item in self.work_queries.list_by_status(
                    user_id=user_id,
                    statuses=("WAITING",),
                    limit=8,
                )
            ],
            "blocked": [
                self._work_item(item)
                for item in self.work_queries.list_by_status(
                    user_id=user_id,
                    statuses=("BLOCKED",),
                    limit=8,
                )
            ],
            "next_actions": [
                self._recommendation(item)
                for item in self.recommendations.recommend(
                    user_id=user_id,
                    today=today,
                    limit=5,
                )
            ],
        }

    def recent_activities(
        self,
        *,
        user_id: str,
        limit: int,
        offset: int,
        project_id: str | None = None,
    ) -> dict[str, Any]:
        parameters: list[Any] = [user_id]
        project_filter = ""
        if project_id is not None:
            project_filter = " AND p.id = ?"
            parameters.append(project_id)
        connection = self.database.connect()
        try:
            base = f"""
                FROM activities a
                JOIN activity_links al
                  ON al.activity_id = a.id
                 AND al.user_id = a.user_id
                 AND al.is_active = 1
                JOIN chat_messages m
                  ON m.id = a.source_message_id
                 AND m.user_id = a.user_id
                JOIN work_items wi
                  ON wi.id = al.work_item_id
                 AND wi.user_id = al.user_id
                JOIN projects p
                  ON p.id = wi.project_id
                 AND p.user_id = wi.user_id
                WHERE a.user_id = ?
                  AND a.validity = 'ACTIVE'
                  AND wi.archived_at IS NULL
                  AND p.archived_at IS NULL
                  {project_filter}
            """
            total = connection.execute(
                f"SELECT COUNT(*) AS count {base}", parameters
            ).fetchone()["count"]
            rows = connection.execute(
                f"""
                SELECT
                    a.id AS activity_id,
                    a.kind,
                    a.summary,
                    a.occurred_on_local,
                    a.recorded_at_utc,
                    p.id AS project_id,
                    p.name AS project_name,
                    wi.id AS work_item_id,
                    wi.title AS work_item_title
                {base}
                ORDER BY a.recorded_at_utc DESC,
                         a.occurred_on_local DESC,
                         m.server_sequence DESC,
                         a.claim_sequence DESC,
                         a.id DESC
                LIMIT ? OFFSET ?
                """,
                (*parameters, limit, offset),
            ).fetchall()
            return {
                "items": [dict(row) for row in rows],
                "total": total,
                "limit": limit,
                "offset": offset,
            }
        finally:
            connection.close()

    def projects(
        self,
        *,
        user_id: str,
        limit: int,
        offset: int,
    ) -> dict[str, Any]:
        connection = self.database.connect()
        try:
            total = connection.execute(
                """
                SELECT COUNT(*) AS count
                FROM projects
                WHERE user_id = ? AND archived_at IS NULL
                """,
                (user_id,),
            ).fetchone()["count"]
            rows = connection.execute(
                """
                SELECT
                    p.id AS project_id,
                    p.id AS id,
                    p.name,
                    p.updated_at,
                    (
                        SELECT COUNT(*)
                        FROM work_items wi
                        WHERE wi.user_id = p.user_id
                          AND wi.project_id = p.id
                          AND wi.archived_at IS NULL
                          AND wi.status <> 'DONE'
                    ) AS current_work_count,
                    (
                        SELECT COUNT(*)
                        FROM work_items wi
                        WHERE wi.user_id = p.user_id
                          AND wi.project_id = p.id
                          AND wi.archived_at IS NULL
                          AND wi.status <> 'DONE'
                    ) AS active_work_count,
                    (
                        SELECT COUNT(*)
                        FROM work_items wi
                        WHERE wi.user_id = p.user_id
                          AND wi.project_id = p.id
                          AND wi.archived_at IS NULL
                          AND wi.status = 'DONE'
                    ) AS completed_work_count,
                    (
                        SELECT MAX(a.recorded_at_utc)
                        FROM work_items wi
                        JOIN activity_links al
                          ON al.work_item_id = wi.id
                         AND al.user_id = wi.user_id
                         AND al.is_active = 1
                        JOIN activities a
                          ON a.id = al.activity_id
                         AND a.user_id = al.user_id
                         AND a.validity = 'ACTIVE'
                        WHERE wi.user_id = p.user_id
                          AND wi.project_id = p.id
                          AND wi.archived_at IS NULL
                    ) AS latest_activity_at
                FROM projects p
                WHERE p.user_id = ?
                  AND p.archived_at IS NULL
                ORDER BY COALESCE(latest_activity_at, p.updated_at) DESC,
                         p.id
                LIMIT ? OFFSET ?
                """,
                (user_id, limit, offset),
            ).fetchall()
            return {
                "items": [dict(row) for row in rows],
                "total": total,
                "limit": limit,
                "offset": offset,
            }
        finally:
            connection.close()

    def project_detail(self, *, user_id: str, project_id: str) -> dict[str, Any]:
        connection = self.database.connect()
        try:
            project = connection.execute(
                """
                SELECT id AS project_id, name, updated_at
                FROM projects
                WHERE id = ? AND user_id = ? AND archived_at IS NULL
                """,
                (project_id, user_id),
            ).fetchone()
            if project is None:
                raise ResourceNotFound("project not found")
            work_rows = connection.execute(
                f"""
                SELECT {StructuredWorkQueryService._work_item_columns()}
                FROM work_items wi
                JOIN projects p
                  ON p.id = wi.project_id
                 AND p.user_id = wi.user_id
                WHERE wi.user_id = ?
                  AND wi.project_id = ?
                  AND wi.archived_at IS NULL
                  AND p.archived_at IS NULL
                ORDER BY
                  CASE WHEN wi.status = 'DONE' THEN 2 ELSE 1 END,
                  {StructuredWorkQueryService._work_order()}
                """,
                (user_id, project_id),
            ).fetchall()
        finally:
            connection.close()

        work_items = [self._work_item(dict(row)) for row in work_rows]
        activities = self.recent_activities(
            user_id=user_id,
            project_id=project_id,
            limit=20,
            offset=0,
        )["items"]
        return {
            **dict(project),
            "current_work_items": [
                item for item in work_items if item["status"] != "DONE"
            ],
            "completed_work_items": [
                item for item in work_items if item["status"] == "DONE"
            ],
            "recent_activities": activities,
        }

    def provider_status(self, *, user_id: str) -> dict[str, Any]:
        provider_name = str(
            getattr(self.extractor, "provider_name", "unknown")
        ).casefold()
        model_name = getattr(self.extractor, "model_name", None)
        checked_at = utc_iso(self.database.clock.now_utc())
        if provider_name in {"local", "api"} and self._provider_run_is_loading(
            user_id,
            provider_name,
        ):
            status = "LOADING"
            message = (
                "Local AI에 연결 중"
                if provider_name == "local"
                else "API AI가 업무 내용을 정리하고 있습니다."
            )
            display_name = "Local AI" if provider_name == "local" else "API AI"
            provider_type = provider_name.upper()
        elif provider_name == "local":
            status, message = self._probe_local(model_name)
            display_name = "Local AI"
            provider_type = "LOCAL"
        elif provider_name == "api":
            status, message = self._probe_api()
            display_name = "API AI"
            provider_type = "API"
        elif provider_name == "deterministic":
            status = "READY"
            message = "테스트용 AI 규칙이 준비되었습니다."
            display_name = "Test AI"
            provider_type = "TEST"
        else:
            status = "READY"
            message = "AI Provider가 준비되었습니다."
            display_name = "AI"
            provider_type = "CUSTOM"
        public_state = (
            status if status in {"READY", "LOADING"} else "ERROR"
        )
        return {
            "provider": provider_name,
            "provider_type": provider_type,
            "label": display_name,
            "display_name": display_name,
            "model": model_name,
            "model_name": model_name,
            "state": public_state,
            "status": status,
            "message": message,
            "checked_at": checked_at,
        }

    def _provider_run_is_loading(self, user_id: str, provider_name: str) -> bool:
        connection = self.database.connect()
        try:
            row = connection.execute(
                """
                SELECT 1
                FROM orchestration_runs
                WHERE user_id = ?
                  AND provider_name = ?
                  AND result_json IS NULL
                  AND status IN ('RECEIVED', 'INTERPRETING')
                LIMIT 1
                """,
                (user_id, provider_name),
            ).fetchone()
            return row is not None
        finally:
            connection.close()

    def _probe_local(self, model_name: str | None) -> tuple[str, str]:
        transport = getattr(self.extractor, "transport", None)
        if transport is None:
            return "READY", self._ready_message("Local AI", model_name)
        try:
            response = transport.client.get(
                f"{transport.base_url}/api/tags",
                timeout=2.0,
            )
            response.raise_for_status()
            body = response.json()
            models = body.get("models", []) if isinstance(body, dict) else []
            available = {
                value
                for item in models
                if isinstance(item, dict)
                for value in (item.get("name"), item.get("model"))
                if isinstance(value, str)
            }
            if model_name and model_name not in available:
                return "DEGRADED", "Local AI 모델을 준비해주세요."
            return "READY", self._ready_message("Local AI", model_name)
        except (httpx.HTTPError, ValueError, TypeError, AttributeError):
            return "UNAVAILABLE", "Local AI 연결에 실패했습니다."

    def _probe_api(self) -> tuple[str, str]:
        transport = getattr(self.extractor, "transport", None)
        if transport is None:
            return "READY", self._ready_message(
                "API AI", getattr(self.extractor, "model_name", None)
            )
        try:
            response = transport.client.get(
                f"{transport.base_url}/models",
                headers={"Authorization": f"Bearer {transport.api_key}"},
                timeout=2.0,
            )
            response.raise_for_status()
            return "READY", self._ready_message(
                "API AI", getattr(self.extractor, "model_name", None)
            )
        except (httpx.HTTPError, ValueError, TypeError, AttributeError):
            return "UNAVAILABLE", "API AI 연결에 실패했습니다."

    @staticmethod
    def _ready_message(display_name: str, model_name: str | None) -> str:
        return f"{display_name} · {model_name}" if model_name else display_name

    @staticmethod
    def _work_item(item: dict[str, Any]) -> dict[str, Any]:
        return {
            "project_id": item["project_id"],
            "project_name": item["project_name"],
            "work_item_id": item["work_item_id"],
            "title": item["title"],
            "status": item["status"],
            "waiting_for": item.get("waiting_for"),
            "blocked_reason": item.get("blocked_reason"),
            "next_action": item.get("next_action"),
            "last_activity_on": item.get("last_activity_on"),
            "completed_at": item.get("completed_at"),
        }

    @staticmethod
    def _recommendation(item: dict[str, Any]) -> dict[str, Any]:
        work_item = item["work_item"]
        return {
            "rank": item["rank"],
            "project_id": work_item["project_id"],
            "project_name": work_item["project_name"],
            "work_item_id": work_item["work_item_id"],
            "title": work_item["title"],
            "status": work_item["status"],
            "recommended_action": item["recommended_action"],
            "reasons": [reason["detail"] for reason in item["reasons"]],
        }
