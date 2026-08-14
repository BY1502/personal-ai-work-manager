from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

from app.database import Database


class StructuredWorkQueryService:
    """Read-only projections sourced exclusively from Structured Work Memory."""

    def __init__(self, database: Database) -> None:
        self.database = database

    def list_by_status(
        self,
        *,
        user_id: str,
        statuses: tuple[str, ...],
        limit: int = 20,
    ) -> list[dict]:
        placeholders = ",".join("?" for _ in statuses)
        connection = self.database.connect()
        try:
            rows = connection.execute(
                f"""
                SELECT {self._work_item_columns()}
                FROM work_items wi
                JOIN projects p
                  ON p.id = wi.project_id
                 AND p.user_id = wi.user_id
                WHERE wi.user_id = ?
                  AND wi.archived_at IS NULL
                  AND p.archived_at IS NULL
                  AND wi.status IN ({placeholders})
                ORDER BY {self._work_order()}
                LIMIT ?
                """,
                (user_id, *statuses, limit),
            ).fetchall()
            return [dict(row) for row in rows]
        finally:
            connection.close()

    def next_actions(self, *, user_id: str, limit: int = 20) -> list[dict]:
        connection = self.database.connect()
        try:
            rows = connection.execute(
                f"""
                SELECT {self._work_item_columns()}
                FROM work_items wi
                JOIN projects p
                  ON p.id = wi.project_id
                 AND p.user_id = wi.user_id
                WHERE wi.user_id = ?
                  AND wi.archived_at IS NULL
                  AND p.archived_at IS NULL
                  AND wi.status NOT IN ('DONE', 'HOLD')
                  AND wi.next_action IS NOT NULL
                  AND length(trim(wi.next_action)) > 0
                ORDER BY {self._work_order()}
                LIMIT ?
                """,
                (user_id, limit),
            ).fetchall()
            return [dict(row) for row in rows]
        finally:
            connection.close()

    def focused_work_item(
        self,
        *,
        user_id: str,
        conversation_id: str,
    ) -> dict | None:
        connection = self.database.connect()
        try:
            row = connection.execute(
                f"""
                SELECT {self._work_item_columns()}
                FROM work_fact_groups wfg
                JOIN orchestration_runs r
                  ON r.id = wfg.run_id
                 AND r.user_id = wfg.user_id
                JOIN chat_messages m
                  ON m.id = wfg.source_message_id
                 AND m.user_id = wfg.user_id
                JOIN work_items wi
                  ON wi.id = wfg.target_work_item_id
                 AND wi.user_id = wfg.user_id
                JOIN projects p
                  ON p.id = wi.project_id
                 AND p.user_id = wi.user_id
                WHERE wfg.user_id = ?
                  AND r.conversation_id = ?
                  AND wfg.status = 'APPLIED'
                  AND wi.archived_at IS NULL
                  AND p.archived_at IS NULL
                ORDER BY m.server_sequence DESC, wfg.group_sequence DESC
                LIMIT 1
                """,
                (user_id, conversation_id),
            ).fetchone()
            if row is None:
                return None
            result = dict(row)
            result["activities"] = self._activities_for_work_item(
                connection, user_id, row["work_item_id"], limit=20
            )
            return result
        finally:
            connection.close()

    def activities_between(
        self,
        *,
        user_id: str,
        start_date: date,
        end_date: date,
        project_id: str | None = None,
        limit: int = 100,
    ) -> list[dict]:
        parameters: list[object] = [
            user_id,
            start_date.isoformat(),
            end_date.isoformat(),
        ]
        project_filter = ""
        if project_id:
            project_filter = " AND p.id = ?"
            parameters.append(project_id)
        parameters.append(limit)
        connection = self.database.connect()
        try:
            rows = connection.execute(
                f"""
                SELECT
                    a.id AS activity_id,
                    a.kind,
                    a.summary,
                    a.occurred_on_local,
                    a.version AS activity_version,
                    al.id AS activity_link_id,
                    al.version AS activity_link_version,
                    p.id AS project_id,
                    p.name AS project_name,
                    wi.id AS work_item_id,
                    wi.title AS work_item_title,
                    wi.status AS current_status
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
                  AND a.occurred_on_local BETWEEN ? AND ?
                  AND a.validity = 'ACTIVE'
                  {project_filter}
                ORDER BY a.occurred_on_local,
                         a.recorded_at_utc,
                         m.server_sequence,
                         a.claim_sequence,
                         a.id
                LIMIT ?
                """,
                parameters,
            ).fetchall()
            return [dict(row) for row in rows]
        finally:
            connection.close()

    def recent_completed(
        self,
        *,
        user_id: str,
        now_utc: datetime,
        days: int = 30,
        limit: int = 20,
    ) -> list[dict]:
        start = now_utc.astimezone(timezone.utc) - timedelta(days=days)
        end = now_utc.astimezone(timezone.utc)
        connection = self.database.connect()
        try:
            rows = connection.execute(
                f"""
                SELECT {self._work_item_columns()}
                FROM work_items wi
                JOIN projects p
                  ON p.id = wi.project_id
                 AND p.user_id = wi.user_id
                WHERE wi.user_id = ?
                  AND wi.archived_at IS NULL
                  AND p.archived_at IS NULL
                  AND wi.status = 'DONE'
                  AND wi.completed_at >= ?
                  AND wi.completed_at <= ?
                ORDER BY wi.completed_at DESC, wi.id
                LIMIT ?
                """,
                (
                    user_id,
                    start.isoformat().replace("+00:00", "Z"),
                    end.isoformat().replace("+00:00", "Z"),
                    limit,
                ),
            ).fetchall()
            return [dict(row) for row in rows]
        finally:
            connection.close()

    @staticmethod
    def _work_item_columns() -> str:
        return """
            p.id AS project_id,
            p.name AS project_name,
            wi.id AS work_item_id,
            wi.title,
            wi.status,
            wi.priority,
            wi.waiting_for,
            wi.blocked_reason,
            wi.next_action,
            wi.last_activity_on,
            wi.completed_at,
            wi.updated_at,
            (
                SELECT a.kind
                FROM activity_links al
                JOIN activities a
                  ON a.id = al.activity_id
                 AND a.user_id = al.user_id
                JOIN chat_messages m
                  ON m.id = a.source_message_id
                 AND m.user_id = a.user_id
                WHERE al.user_id = wi.user_id
                  AND al.work_item_id = wi.id
                  AND al.is_active = 1
                  AND a.validity = 'ACTIVE'
                ORDER BY a.occurred_on_local DESC,
                         a.recorded_at_utc DESC,
                         m.server_sequence DESC,
                         a.claim_sequence DESC,
                         a.id DESC
                LIMIT 1
            ) AS latest_activity_kind
        """

    @staticmethod
    def _work_order() -> str:
        return """
            CASE wi.priority WHEN 'HIGH' THEN 1 WHEN 'NORMAL' THEN 2 ELSE 3 END,
            COALESCE(wi.last_activity_on, '0001-01-01'),
            wi.id
        """

    @staticmethod
    def _activities_for_work_item(
        connection,
        user_id: str,
        work_item_id: str,
        *,
        limit: int,
    ) -> list[dict]:
        rows = connection.execute(
            """
            SELECT a.id AS activity_id,
                   a.kind,
                   a.summary,
                   a.occurred_on_local
            FROM activities a
            JOIN activity_links al
              ON al.activity_id = a.id
             AND al.user_id = a.user_id
             AND al.is_active = 1
            JOIN chat_messages m
              ON m.id = a.source_message_id
             AND m.user_id = a.user_id
            WHERE a.user_id = ?
              AND al.work_item_id = ?
              AND a.validity = 'ACTIVE'
            ORDER BY a.occurred_on_local,
                     a.recorded_at_utc,
                     m.server_sequence,
                     a.claim_sequence,
                     a.id
            LIMIT ?
            """,
            (user_id, work_item_id, limit),
        ).fetchall()
        return [dict(row) for row in rows]
