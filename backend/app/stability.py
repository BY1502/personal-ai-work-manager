from __future__ import annotations

import json
from collections import Counter
from math import ceil
from typing import Any

from app.database import Database
from app.utils import utc_iso


STABILITY_AUDIT_SCHEMA_VERSION = "stability-audit.v1"


class StabilityAuditService:
    """Read-only operational checks for the Phase 1 stabilization period.

    The report deliberately contains aggregate counts instead of message text,
    entity IDs, provider responses, or model reasoning. Operators can use the
    persisted run/fact/audit records for a focused investigation after a signal
    is raised.
    """

    def __init__(self, database: Database) -> None:
        self.database = database

    def inspect(self, *, user_id: str) -> dict[str, Any]:
        connection = self.database.connect()
        try:
            connection.execute("PRAGMA query_only = ON")
            checks = [
                self._sqlite_integrity(connection),
                self._foreign_keys(connection),
                self._count_check(
                    connection,
                    code="DUPLICATE_ACTIVE_PROJECT",
                    query="""
                        SELECT COUNT(*)
                        FROM (
                            SELECT normalized_name
                            FROM projects
                            WHERE user_id = ? AND archived_at IS NULL
                            GROUP BY normalized_name
                            HAVING COUNT(*) > 1
                        )
                    """,
                    parameters=(user_id,),
                    summary="같은 정규화 이름을 가진 활성 Project 그룹",
                ),
                self._count_check(
                    connection,
                    code="DUPLICATE_ACTIVE_WORK_ITEM",
                    query="""
                        SELECT COUNT(*)
                        FROM (
                            SELECT project_id, normalized_title
                            FROM work_items
                            WHERE user_id = ? AND archived_at IS NULL
                            GROUP BY project_id, normalized_title
                            HAVING COUNT(*) > 1
                        )
                    """,
                    parameters=(user_id,),
                    summary="같은 Project 안의 중복 활성 Work Item 그룹",
                ),
                self._count_check(
                    connection,
                    code="INVALID_WORK_STATE",
                    query="""
                        SELECT COUNT(*)
                        FROM work_items
                        WHERE user_id = ?
                          AND archived_at IS NULL
                          AND (
                            (status = 'WAITING' AND
                                (waiting_for IS NULL OR length(trim(waiting_for)) = 0))
                            OR (status <> 'WAITING' AND waiting_for IS NOT NULL)
                            OR (status = 'BLOCKED' AND
                                (blocked_reason IS NULL OR
                                 length(trim(blocked_reason)) = 0))
                            OR (status <> 'BLOCKED' AND blocked_reason IS NOT NULL)
                            OR (status = 'DONE' AND
                                (completed_at IS NULL OR next_action IS NOT NULL OR
                                 waiting_for IS NOT NULL OR blocked_reason IS NOT NULL))
                            OR (status <> 'DONE' AND completed_at IS NOT NULL)
                          )
                    """,
                    parameters=(user_id,),
                    summary="WAITING/BLOCKED/DONE 필드 불변식을 벗어난 Work Item",
                ),
                self._count_check(
                    connection,
                    code="INVALID_ACTIVE_ACTIVITY_LINK",
                    query="""
                        SELECT COUNT(*)
                        FROM (
                            SELECT a.id
                            FROM activities a
                            LEFT JOIN activity_links al
                              ON al.activity_id = a.id
                             AND al.user_id = a.user_id
                             AND al.is_active = 1
                            WHERE a.user_id = ? AND a.validity = 'ACTIVE'
                            GROUP BY a.id
                            HAVING COUNT(al.id) <> 1
                        )
                    """,
                    parameters=(user_id,),
                    summary="활성 Link가 정확히 하나가 아닌 활성 Activity",
                ),
                self._count_check(
                    connection,
                    code="APPLIED_GROUP_WITHOUT_RECEIPT",
                    query="""
                        SELECT COUNT(*)
                        FROM work_fact_groups wfg
                        LEFT JOIN change_receipts cr
                          ON cr.fact_group_id = wfg.id
                         AND cr.user_id = wfg.user_id
                        WHERE wfg.user_id = ?
                          AND wfg.status = 'APPLIED'
                          AND cr.id IS NULL
                    """,
                    parameters=(user_id,),
                    summary="Change Receipt가 없는 APPLIED Fact Group",
                ),
                self._report_coverage_check(connection, user_id=user_id),
            ]
            signals = self._signals(connection, user_id=user_id)
        finally:
            connection.close()

        attention_count = sum(
            1 for check in checks if check["status"] != "PASS"
        )
        return {
            "schema_version": STABILITY_AUDIT_SCHEMA_VERSION,
            "generated_at": utc_iso(self.database.clock.now_utc()),
            "health": "PASS" if attention_count == 0 else "ATTENTION",
            "attention_count": attention_count,
            "checks": checks,
            "signals": signals,
        }

    @staticmethod
    def _sqlite_integrity(connection) -> dict[str, Any]:
        rows = [row[0] for row in connection.execute("PRAGMA integrity_check")]
        failures = [value for value in rows if value.casefold() != "ok"]
        return {
            "code": "SQLITE_INTEGRITY",
            "status": "PASS" if not failures else "ATTENTION",
            "count": len(failures),
            "summary": "SQLite integrity_check 오류",
        }

    @staticmethod
    def _foreign_keys(connection) -> dict[str, Any]:
        count = len(connection.execute("PRAGMA foreign_key_check").fetchall())
        return {
            "code": "FOREIGN_KEY_VIOLATION",
            "status": "PASS" if count == 0 else "ATTENTION",
            "count": count,
            "summary": "Foreign key 위반",
        }

    @staticmethod
    def _count_check(
        connection,
        *,
        code: str,
        query: str,
        parameters: tuple[Any, ...],
        summary: str,
    ) -> dict[str, Any]:
        count = int(connection.execute(query, parameters).fetchone()[0])
        return {
            "code": code,
            "status": "PASS" if count == 0 else "ATTENTION",
            "count": count,
            "summary": summary,
        }

    @staticmethod
    def _report_coverage_check(connection, *, user_id: str) -> dict[str, Any]:
        invalid = 0
        invariant_fields = (
            "missing_source_activity_count",
            "unexpected_activity_count",
            "duplicate_inclusion_count",
            "summary_index_mismatch_count",
            "summary_index_duplicate_count",
            "source_duplicate_count",
        )
        rows = connection.execute(
            """
            SELECT diagnostics_json
            FROM report_snapshots
            WHERE user_id = ?
            """,
            (user_id,),
        ).fetchall()
        for row in rows:
            try:
                diagnostic = json.loads(row["diagnostics_json"])
            except (TypeError, json.JSONDecodeError):
                invalid += 1
                continue
            if any(int(diagnostic.get(field, 0) or 0) != 0 for field in invariant_fields):
                invalid += 1
        return {
            "code": "REPORT_ACTIVITY_COVERAGE",
            "status": "PASS" if invalid == 0 else "ATTENTION",
            "count": invalid,
            "summary": "Activity 누락·중복 진단이 발생한 Report Snapshot",
        }

    @staticmethod
    def _signals(connection, *, user_id: str) -> dict[str, Any]:
        link_decisions: Counter[str] = Counter()
        link_policies: Counter[str] = Counter()
        decision_rows = connection.execute(
            """
            SELECT decision_json
            FROM work_fact_groups
            WHERE user_id = ? AND decision_json IS NOT NULL
            """,
            (user_id,),
        ).fetchall()
        for row in decision_rows:
            try:
                decision = json.loads(row["decision_json"])
            except (TypeError, json.JSONDecodeError):
                link_decisions["INVALID_DECISION_JSON"] += 1
                continue
            link_decisions[str(decision.get("decision", "UNKNOWN"))] += 1
            link_policies[str(decision.get("policy_version", "UNKNOWN"))] += 1

        failed_runs = {
            str(row["error_code"] or "UNKNOWN"): int(row["count"])
            for row in connection.execute(
                """
                SELECT error_code, COUNT(*) AS count
                FROM orchestration_runs
                WHERE user_id = ? AND status = 'FAILED'
                GROUP BY error_code
                ORDER BY error_code
                """,
                (user_id,),
            ).fetchall()
        }
        event_counts = {
            str(row["event_type"]): int(row["count"])
            for row in connection.execute(
                """
                SELECT event_type, COUNT(*) AS count
                FROM execution_events
                WHERE user_id = ?
                GROUP BY event_type
                ORDER BY event_type
                """,
                (user_id,),
            ).fetchall()
        }
        provider_durations: list[int] = []
        for row in connection.execute(
            """
            SELECT payload_json
            FROM execution_events
            WHERE user_id = ? AND event_type = 'INTERPRETATION_COMPLETED'
            """,
            (user_id,),
        ).fetchall():
            try:
                duration = json.loads(row["payload_json"]).get("duration_ms")
            except (TypeError, json.JSONDecodeError):
                continue
            if isinstance(duration, int) and duration >= 0:
                provider_durations.append(duration)
        report_counts = {
            "total": 0,
            "stale": 0,
            "template_fallback": 0,
        }
        report_row = connection.execute(
            """
            SELECT
                COUNT(*) AS total,
                SUM(CASE WHEN freshness = 'STALE' THEN 1 ELSE 0 END) AS stale,
                SUM(CASE WHEN generation_mode = 'TEMPLATE_FALLBACK'
                         THEN 1 ELSE 0 END) AS template_fallback
            FROM report_snapshots
            WHERE user_id = ?
            """,
            (user_id,),
        ).fetchone()
        if report_row:
            report_counts = {
                "total": int(report_row["total"] or 0),
                "stale": int(report_row["stale"] or 0),
                "template_fallback": int(report_row["template_fallback"] or 0),
            }
        report_diagnostics = {
            str(row["generation_diagnostic"]): int(row["count"])
            for row in connection.execute(
                """
                SELECT generation_diagnostic, COUNT(*) AS count
                FROM report_snapshots
                WHERE user_id = ?
                GROUP BY generation_diagnostic
                ORDER BY generation_diagnostic
                """,
                (user_id,),
            ).fetchall()
        }
        narration_durations = [
            int(row["narration_duration_ms"])
            for row in connection.execute(
                """
                SELECT narration_duration_ms
                FROM report_snapshots
                WHERE user_id = ? AND narration_duration_ms IS NOT NULL
                """,
                (user_id,),
            ).fetchall()
            if int(row["narration_duration_ms"]) >= 0
        ]

        open_clarifications = int(
            connection.execute(
                """
                SELECT COUNT(*)
                FROM clarifications
                WHERE user_id = ? AND status = 'OPEN'
                """,
                (user_id,),
            ).fetchone()[0]
        )
        potential_duplicate_activity_groups = int(
            connection.execute(
                """
                SELECT COUNT(*)
                FROM (
                    SELECT source_excerpt_hash, kind, occurred_on_local
                    FROM activities
                    WHERE user_id = ? AND validity = 'ACTIVE'
                    GROUP BY source_excerpt_hash, kind, occurred_on_local
                    HAVING COUNT(*) > 1
                )
                """,
                (user_id,),
            ).fetchone()[0]
        )
        active_next_actions = int(
            connection.execute(
                """
                SELECT COUNT(*)
                FROM work_items
                WHERE user_id = ?
                  AND archived_at IS NULL
                  AND status NOT IN ('DONE', 'HOLD')
                  AND next_action IS NOT NULL
                  AND length(trim(next_action)) > 0
                """,
                (user_id,),
            ).fetchone()[0]
        )
        return {
            "link_decisions": dict(sorted(link_decisions.items())),
            "link_policy_versions": dict(sorted(link_policies.items())),
            "failed_runs_by_error": failed_runs,
            "execution_events": event_counts,
            "provider_latency_ms": StabilityAuditService._duration_summary(
                provider_durations
            ),
            "reports": report_counts,
            "report_generation_diagnostics": report_diagnostics,
            "report_narration_latency_ms": (
                StabilityAuditService._duration_summary(narration_durations)
            ),
            "open_clarifications": open_clarifications,
            "potential_duplicate_activity_groups": (
                potential_duplicate_activity_groups
            ),
            "active_next_actions": active_next_actions,
        }

    @staticmethod
    def _duration_summary(values: list[int]) -> dict[str, int]:
        if not values:
            return {"samples": 0, "p95": 0, "max": 0}
        ordered = sorted(values)
        p95_index = max(0, ceil(len(ordered) * 0.95) - 1)
        return {
            "samples": len(ordered),
            "p95": ordered[p95_index],
            "max": ordered[-1],
        }
