from __future__ import annotations

import json
import re
import sqlite3
import time as monotonic_time
from collections import defaultdict
from datetime import date, datetime, time, timedelta, timezone
from enum import StrEnum
from typing import Any, Protocol
from zoneinfo import ZoneInfo

from app.database import Database
from app.providers import ExtractionTimeoutError, ExtractionTransportError
from app.utils import canonical_json, new_id, normalize_name, sha256_text, utc_iso


REPORT_POLICY_VERSION = "report-fold-v1"
REPORT_SCHEMA_VERSION = "report-structured.v1"
REPORT_SOURCE_SCHEMA_VERSION = "report-sources.v1"
REPORT_NARRATION_INPUT_VERSION = "report-narration-input.v1"
REPORT_NARRATION_OUTPUT_VERSION = "report-narration.v1"
REPORT_DIAGNOSTICS_VERSION = "report-diagnostics.v1"


class ReportType(StrEnum):
    DAILY = "DAILY"
    WEEKLY = "WEEKLY"
    PROJECT = "PROJECT"
    RANGE = "RANGE"


class ReportValidationError(ValueError):
    pass


class ReportNotFound(LookupError):
    pass


class ReportNarrator(Protocol):
    """Optional natural-language renderer for already selected report facts."""

    provider_name: str
    model_name: str | None
    prompt_version: str

    def narrate(self, payload: dict[str, Any]) -> dict[str, Any]: ...


class ReportManager:
    """Builds grounded reports from Structured Work Memory only.

    The manager deliberately has no access to chat message content. Activity facts,
    active links, Work Item snapshots, and Work Item change audits are the only
    report sources. The deterministic template remains authoritative; an optional
    narrator may only replace text after exact fact-binding validation.
    """

    def __init__(
        self,
        database: Database,
        *,
        waiting_followup_days: int = 3,
        narrator: ReportNarrator | None = None,
    ) -> None:
        if waiting_followup_days < 1:
            raise ValueError("waiting_followup_days must be positive")
        self.database = database
        self.waiting_followup_days = waiting_followup_days
        self.narrator = narrator

    def generate(
        self,
        user_id: str,
        report_type: str | ReportType,
        today: date,
        project_mention: str | None = None,
        start_date: date | str | None = None,
        end_date: date | str | None = None,
        source_run_id: str | None = None,
    ) -> dict[str, Any]:
        resolved_type = self._report_type(report_type)
        resolved_today = self._date_value(today, field="today")

        if source_run_id:
            existing = self._find_by_source_run(
                user_id=user_id,
                source_run_id=source_run_id,
            )
            if existing is not None:
                return existing

        as_of_utc = utc_iso(self.database.clock.now_utc())
        connection = self.database.connect()
        try:
            connection.execute("BEGIN")
            timezone_name = self._user_timezone(connection, user_id)
            project = self._resolve_project(
                connection,
                user_id=user_id,
                project_mention=project_mention,
                required=resolved_type == ReportType.PROJECT,
            )
            period_start, period_end = self._resolve_period(
                report_type=resolved_type,
                today=resolved_today,
                start_date=start_date,
                end_date=end_date,
                timezone_name=timezone_name,
                project=project,
            )
            collected = self._collect_sources(
                connection,
                user_id=user_id,
                project_id=(project["id"] if project else None),
                period_start=period_start,
                period_end=period_end,
                today=resolved_today,
                timezone_name=timezone_name,
            )
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

        period = {
            "start_date": period_start.isoformat(),
            "end_date": period_end.isoformat(),
            "timezone": timezone_name,
        }
        structured = self._build_structured_report(
            report_type=resolved_type,
            period=period,
            as_of_utc=as_of_utc,
            project=project,
            summaries=collected["summaries"],
            current_exceptions=collected["current_exceptions"],
        )
        source_manifest = {
            "schema_version": REPORT_SOURCE_SCHEMA_VERSION,
            "as_of_utc": as_of_utc,
            **collected["manifest"],
        }
        source_digest = sha256_text(canonical_json(source_manifest))
        (
            rendered_text,
            generation_mode,
            generation_diagnostic,
            narration_duration_ms,
        ) = self._render(
            structured=structured,
            source_digest=source_digest,
        )
        diagnostics = self._report_diagnostics(
            structured=structured,
            source_manifest=source_manifest,
            generation_diagnostic=generation_diagnostic,
            narration_duration_ms=narration_duration_ms,
        )
        self._require_complete_activity_coverage(diagnostics)
        report_id = new_id("report")
        project_id = project["id"] if project else None

        if source_run_id:
            self._require_source_run(user_id=user_id, source_run_id=source_run_id)

        initial_freshness = "FRESH"
        try:
            with self.database.transaction() as write_connection:
                # The source read and optional narration deliberately happen without
                # a write lock. Revalidate under the same write transaction as the
                # snapshot insert so a correction in that gap is visible from the
                # first response and cannot race the persisted freshness value.
                initial_freshness = self._manifest_freshness(
                    write_connection,
                    user_id=user_id,
                    manifest=source_manifest,
                )
                write_connection.execute(
                    """
                    INSERT INTO report_snapshots(
                        id,
                        user_id,
                        source_run_id,
                        report_type,
                        project_id,
                        period_start_local,
                        period_end_local,
                        timezone,
                        as_of_utc,
                        structured_sections_json,
                        rendered_text,
                        source_manifest_json,
                        source_digest,
                        freshness,
                        generation_mode,
                        generation_diagnostic,
                        narration_duration_ms,
                        diagnostics_json,
                        policy_version,
                        narrator_provider,
                        model_version,
                        prompt_version,
                        created_at
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                            ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        report_id,
                        user_id,
                        source_run_id,
                        resolved_type.value,
                        project_id,
                        period_start.isoformat(),
                        period_end.isoformat(),
                        timezone_name,
                        as_of_utc,
                        canonical_json(structured),
                        rendered_text,
                        canonical_json(source_manifest),
                        source_digest,
                        initial_freshness,
                        generation_mode,
                        generation_diagnostic,
                        narration_duration_ms,
                        canonical_json(diagnostics),
                        REPORT_POLICY_VERSION,
                        getattr(self.narrator, "provider_name", None),
                        getattr(self.narrator, "model_name", None),
                        getattr(self.narrator, "prompt_version", None),
                        as_of_utc,
                    ),
                )
        except sqlite3.IntegrityError:
            if source_run_id:
                existing = self._find_by_source_run(
                    user_id=user_id,
                    source_run_id=source_run_id,
                )
                if existing is not None:
                    return existing
            raise

        return {
            "report_id": report_id,
            "report_type": resolved_type.value,
            "period": period,
            "project": structured["project"],
            "sections": structured["sections"],
            "work_items": structured["work_items"],
            "current_exceptions": structured["current_exceptions"],
            "rendered_text": rendered_text,
            "source_digest": source_digest,
            "freshness": initial_freshness,
            "generation_mode": generation_mode,
            "generation_diagnostic": generation_diagnostic,
            "narration_duration_ms": narration_duration_ms,
            "diagnostics": diagnostics,
            "policy_version": REPORT_POLICY_VERSION,
            "as_of_utc": as_of_utc,
        }

    def get_report(self, *, user_id: str, report_id: str) -> dict[str, Any]:
        connection = self.database.connect()
        try:
            row = connection.execute(
                """
                SELECT *
                FROM report_snapshots
                WHERE id = ? AND user_id = ?
                """,
                (report_id, user_id),
            ).fetchone()
            if row is None:
                raise ReportNotFound("report snapshot was not found")
            freshness = (
                "STALE"
                if row["freshness"] == "STALE"
                else self._manifest_freshness(
                    connection,
                    user_id=user_id,
                    manifest=json.loads(row["source_manifest_json"]),
                )
            )
        finally:
            connection.close()

        if freshness == "STALE" and row["freshness"] != "STALE":
            with self.database.transaction() as write_connection:
                write_connection.execute(
                    """
                    UPDATE report_snapshots
                    SET freshness = 'STALE'
                    WHERE id = ? AND user_id = ?
                    """,
                    (report_id, user_id),
                )
        result = self._snapshot_result(row)
        result["freshness"] = freshness
        return result

    def list_reports(self, *, user_id: str, limit: int = 50) -> list[dict[str, Any]]:
        if not 1 <= limit <= 200:
            raise ReportValidationError("limit must be between 1 and 200")
        connection = self.database.connect()
        try:
            rows = connection.execute(
                """
                SELECT *
                FROM report_snapshots
                WHERE user_id = ?
                ORDER BY created_at DESC, id DESC
                LIMIT ?
                """,
                (user_id, limit),
            ).fetchall()
            results: list[dict[str, Any]] = []
            stale_ids: list[str] = []
            for row in rows:
                freshness = (
                    "STALE"
                    if row["freshness"] == "STALE"
                    else self._manifest_freshness(
                        connection,
                        user_id=user_id,
                        manifest=json.loads(row["source_manifest_json"]),
                    )
                )
                result = self._snapshot_result(row)
                result["freshness"] = freshness
                results.append(result)
                if freshness == "STALE" and row["freshness"] != "STALE":
                    stale_ids.append(row["id"])
        finally:
            connection.close()
        if stale_ids:
            with self.database.transaction() as write_connection:
                write_connection.executemany(
                    """
                    UPDATE report_snapshots
                    SET freshness = 'STALE'
                    WHERE id = ? AND user_id = ?
                    """,
                    [(report_id, user_id) for report_id in stale_ids],
                )
        return results

    def _collect_sources(
        self,
        connection,
        *,
        user_id: str,
        project_id: str | None,
        period_start: date,
        period_end: date,
        today: date,
        timezone_name: str,
    ) -> dict[str, Any]:
        start_utc, end_exclusive_utc = self._utc_bounds(
            period_start,
            period_end,
            timezone_name,
        )
        project_filter = ""
        activity_parameters: list[Any] = [
            user_id,
            period_start.isoformat(),
            period_end.isoformat(),
        ]
        audit_parameters: list[Any] = [user_id, utc_iso(end_exclusive_utc)]
        if project_id:
            project_filter = " AND p.id = ?"
            activity_parameters.append(project_id)
            audit_parameters.append(project_id)

        activities = [
            dict(row)
            for row in connection.execute(
                f"""
                SELECT
                    a.id AS activity_id,
                    a.kind,
                    a.summary,
                    a.occurred_on_local,
                    a.recorded_at_utc,
                    a.claim_sequence,
                    a.version AS activity_version,
                    a.validity,
                    al.id AS link_id,
                    al.version AS link_version,
                    al.work_item_id,
                    p.id AS project_id,
                    p.name AS project_name
                FROM activities a
                JOIN activity_links al
                  ON al.activity_id = a.id
                 AND al.user_id = a.user_id
                 AND al.is_active = 1
                JOIN work_items wi
                  ON wi.id = al.work_item_id
                 AND wi.user_id = al.user_id
                JOIN projects p
                  ON p.id = wi.project_id
                 AND p.user_id = wi.user_id
                JOIN chat_messages m
                  ON m.id = a.source_message_id
                 AND m.user_id = a.user_id
                WHERE a.user_id = ?
                  AND a.occurred_on_local BETWEEN ? AND ?
                  AND a.validity = 'ACTIVE'
                  {project_filter}
                ORDER BY a.occurred_on_local,
                         a.recorded_at_utc,
                         m.conversation_id,
                         m.server_sequence,
                         a.claim_sequence,
                         a.id
                """,
                activity_parameters,
            ).fetchall()
        ]

        audits: list[dict[str, Any]] = []
        for row in connection.execute(
            f"""
            SELECT ca.*
            FROM change_audit ca
            JOIN work_items wi
              ON wi.id = ca.target_id
             AND wi.user_id = ca.user_id
            JOIN projects p
              ON p.id = wi.project_id
             AND p.user_id = wi.user_id
            WHERE ca.user_id = ?
              AND ca.target_type = 'WORK_ITEM'
              AND ca.created_at < ?
              {project_filter}
            ORDER BY ca.target_id,
                     COALESCE(ca.applied_version, 0),
                     ca.created_at,
                     ca.id
            """,
            audit_parameters,
        ).fetchall():
            parsed = dict(row)
            parsed["before"] = self._json_object(parsed.pop("before_json"))
            parsed["after"] = self._json_object(parsed.pop("after_json"))
            parsed["occurred_at"] = self._utc_datetime(parsed["created_at"])
            if self._audit_changes_report_fact(parsed):
                audits.append(parsed)

        touched_work_items = {item["work_item_id"] for item in activities}
        touched_work_items.update(
            audit["target_id"]
            for audit in audits
            if start_utc <= audit["occurred_at"] < end_exclusive_utc
        )
        snapshots = self._work_item_snapshots(
            connection,
            user_id=user_id,
            work_item_ids=touched_work_items,
        )

        activities_by_work_item: dict[str, list[dict]] = defaultdict(list)
        for activity in activities:
            activities_by_work_item[activity["work_item_id"]].append(activity)
        audits_by_work_item: dict[str, list[dict]] = defaultdict(list)
        for audit in audits:
            audits_by_work_item[audit["target_id"]].append(audit)

        summaries: list[dict[str, Any]] = []
        selected_audits: dict[str, dict] = {}
        for work_item_id in sorted(
            touched_work_items,
            key=lambda item_id: (
                snapshots[item_id]["project_name"],
                snapshots[item_id]["title"],
                item_id,
            ),
        ):
            summary, used_audits = self._fold_work_item(
                snapshot=snapshots[work_item_id],
                activities=activities_by_work_item[work_item_id],
                audits=audits_by_work_item[work_item_id],
                start_utc=start_utc,
                end_exclusive_utc=end_exclusive_utc,
            )
            summaries.append(summary)
            for audit in used_audits:
                selected_audits[audit["id"]] = audit

        exceptions = self._current_exceptions(
            connection,
            user_id=user_id,
            project_id=project_id,
            touched_work_items=touched_work_items,
            today=today,
            timezone_name=timezone_name,
        )
        manifest_snapshots = {
            snapshot["work_item_id"]: snapshot for snapshot in snapshots.values()
        }
        for exception in exceptions:
            manifest_snapshots[exception["work_item_id"]] = exception

        manifest = self._source_manifest(
            activities=activities,
            audits=list(selected_audits.values()),
            work_items=list(manifest_snapshots.values()),
        )
        return {
            "summaries": summaries,
            "current_exceptions": exceptions,
            "manifest": manifest,
        }

    def _fold_work_item(
        self,
        *,
        snapshot: dict,
        activities: list[dict],
        audits: list[dict],
        start_utc: datetime,
        end_exclusive_utc: datetime,
    ) -> tuple[dict[str, Any], list[dict]]:
        ordered_audits = sorted(
            audits,
            key=lambda audit: (
                audit["applied_version"] or 0,
                audit["occurred_at"],
                audit["id"],
            ),
        )
        prior = [audit for audit in ordered_audits if audit["occurred_at"] < start_utc]
        during = [
            audit
            for audit in ordered_audits
            if start_utc <= audit["occurred_at"] < end_exclusive_utc
        ]
        last_prior = prior[-1] if prior else None
        start_status = self._status_after(last_prior)
        if start_status is None and during:
            start_status = (during[0]["before"] or {}).get("status")
        end_status = self._status_after(during[-1] if during else last_prior)

        transitions: list[dict[str, Any]] = []
        for audit in during:
            before = audit["before"] or {}
            after = audit["after"] or {}
            changes = {
                field: {"before": before.get(field), "after": after.get(field)}
                for field in self._tracked_audit_fields()
                if before.get(field) != after.get(field)
            }
            transitions.append(
                {
                    "audit_id": audit["id"],
                    "occurred_at_utc": audit["created_at"],
                    "changes": changes,
                }
            )

        grouped_activities: dict[tuple[str, str], dict[str, Any]] = {}
        for activity in activities:
            key = (
                activity["kind"],
                self._normalize_activity_summary(activity["summary"]),
            )
            if key not in grouped_activities:
                grouped_activities[key] = {
                    "kind": activity["kind"],
                    "summary": activity["summary"],
                    "dates": [],
                    "activity_ids": [],
                }
            group = grouped_activities[key]
            if activity["occurred_on_local"] not in group["dates"]:
                group["dates"].append(activity["occurred_on_local"])
            group["activity_ids"].append(activity["activity_id"])

        activity_groups = list(grouped_activities.values())
        outcomes = self._outcome_codes(activities, transitions)
        used_audits = ([last_prior] if last_prior else []) + during
        source_refs = [
            *(f"activity:{activity['activity_id']}" for activity in activities),
            *(f"audit:{audit['id']}" for audit in used_audits),
            f"work_item:{snapshot['work_item_id']}",
        ]
        current_status = snapshot["status"]
        return (
            {
                "project_id": snapshot["project_id"],
                "project_name": snapshot["project_name"],
                "work_item_id": snapshot["work_item_id"],
                "work_item_title": snapshot["title"],
                "start_status": start_status,
                "end_status": end_status,
                "current_status": current_status,
                "activity_groups": activity_groups,
                "distinct_activity_ids": [
                    activity["activity_id"] for activity in activities
                ],
                "status_transitions": transitions,
                "outcome_codes": outcomes,
                "net_outcome": outcomes[0] if outcomes else "UPDATED",
                "open_waiting": (
                    snapshot["waiting_for"]
                    if current_status == "WAITING"
                    else None
                ),
                "open_blocked": (
                    snapshot["blocked_reason"]
                    if current_status == "BLOCKED"
                    else None
                ),
                "next_action": (
                    snapshot["next_action"]
                    if current_status != "DONE"
                    else None
                ),
                "source_fact_ids": source_refs,
            },
            used_audits,
        )

    def _build_structured_report(
        self,
        *,
        report_type: ReportType,
        period: dict[str, str],
        as_of_utc: str,
        project: dict | None,
        summaries: list[dict[str, Any]],
        current_exceptions: list[dict[str, Any]],
    ) -> dict[str, Any]:
        sections: dict[str, list[dict[str, Any]]] = {
            "major_work": [],
            "completed_work": [],
            "in_progress_work": [],
            "issues": [],
            "next_actions": [],
            "current_exceptions": [],
        }
        for summary in summaries:
            refs = summary["source_fact_ids"]
            sections["major_work"].append(
                self._section_item(
                    "major_work", summary, self._major_text(summary), refs
                )
            )
            if "COMPLETED" in summary["outcome_codes"]:
                sections["completed_work"].append(
                    self._section_item(
                        "completed_work",
                        summary,
                        f"{summary['project_name']}의 "
                        f"‘{summary['work_item_title']}’ 업무를 완료했습니다.",
                        refs,
                    )
                )
            if summary["current_status"] == "IN_PROGRESS":
                sections["in_progress_work"].append(
                    self._section_item(
                        "in_progress_work",
                        summary,
                        f"{summary['project_name']}의 "
                        f"‘{summary['work_item_title']}’ 업무를 진행 중입니다.",
                        refs,
                    )
                )
            issue = self._issue_text(summary)
            if issue:
                sections["issues"].append(
                    self._section_item("issues", summary, issue, refs)
                )
            if summary["next_action"]:
                sections["next_actions"].append(
                    self._section_item(
                        "next_actions",
                        summary,
                        f"{summary['project_name']}의 "
                        f"‘{summary['work_item_title']}’ 업무에서 다음에 할 일은 "
                        f"‘{summary['next_action']}’입니다.",
                        refs,
                    )
                )

        for item in current_exceptions:
            text = self._snapshot_issue_text(item)
            if text:
                sections["current_exceptions"].append(
                    {
                        "bullet_id": (
                            f"current_exceptions:{item['work_item_id']}"
                        ),
                        "project_id": item["project_id"],
                        "work_item_id": item["work_item_id"],
                        "text": text,
                        "source_fact_ids": [
                            f"work_item:{item['work_item_id']}"
                        ],
                    }
                )

        return {
            "schema_version": REPORT_SCHEMA_VERSION,
            "report_type": report_type.value,
            "period": period,
            "as_of_utc": as_of_utc,
            "project": (
                {"project_id": project["id"], "project_name": project["name"]}
                if project
                else None
            ),
            "work_items": summaries,
            "current_exceptions": current_exceptions,
            "sections": sections,
        }

    def _render(
        self,
        *,
        structured: dict[str, Any],
        source_digest: str,
    ) -> tuple[str, str, str, int | None]:
        if self.narrator is None:
            return self._render_template(structured), "TEMPLATE", "NONE", None

        payload = self._narration_payload(
            structured=structured,
            source_digest=source_digest,
        )
        if not payload["bullets"]:
            return self._render_template(structured), "TEMPLATE", "NONE", None
        started_at = monotonic_time.monotonic()
        try:
            candidate = self.narrator.narrate(payload)
            overrides = self._validate_narration(
                candidate=candidate,
                payload=payload,
                structured=structured,
            )
        except (ExtractionTimeoutError, TimeoutError):
            return (
                self._render_template(structured),
                "TEMPLATE_FALLBACK",
                "NARRATOR_TIMEOUT",
                self._elapsed_ms(started_at),
            )
        except ExtractionTransportError:
            return (
                self._render_template(structured),
                "TEMPLATE_FALLBACK",
                "NARRATOR_UNAVAILABLE",
                self._elapsed_ms(started_at),
            )
        except (ReportValidationError, ValueError, TypeError):
            return (
                self._render_template(structured),
                "TEMPLATE_FALLBACK",
                "NARRATOR_OUTPUT_REJECTED",
                self._elapsed_ms(started_at),
            )
        except Exception:
            return (
                self._render_template(structured),
                "TEMPLATE_FALLBACK",
                "NARRATOR_FAILED",
                self._elapsed_ms(started_at),
            )
        return (
            self._render_template(structured, text_overrides=overrides),
            "LLM",
            "NONE",
            self._elapsed_ms(started_at),
        )

    @staticmethod
    def _elapsed_ms(started_at: float) -> int:
        return max(0, round((monotonic_time.monotonic() - started_at) * 1_000))

    @staticmethod
    def _report_diagnostics(
        *,
        structured: dict[str, Any],
        source_manifest: dict[str, Any],
        generation_diagnostic: str,
        narration_duration_ms: int | None,
    ) -> dict[str, Any]:
        source_ids = [item["id"] for item in source_manifest.get("activities", [])]
        grouped_ids: list[str] = []
        distinct_ids: list[str] = []
        folded_group_count = 0
        for work_item in structured["work_items"]:
            groups = work_item.get("activity_groups", [])
            folded_group_count += len(groups)
            for group in groups:
                grouped_ids.extend(group.get("activity_ids", []))
            distinct_ids.extend(work_item.get("distinct_activity_ids", []))

        source_set = set(source_ids)
        grouped_set = set(grouped_ids)
        distinct_set = set(distinct_ids)
        return {
            "schema_version": REPORT_DIAGNOSTICS_VERSION,
            "source_activity_count": len(source_ids),
            "included_activity_count": len(grouped_ids),
            "distinct_activity_count": len(grouped_set),
            "folded_activity_group_count": folded_group_count,
            "work_item_count": len(structured["work_items"]),
            "missing_source_activity_count": len(source_set - grouped_set),
            "unexpected_activity_count": len(grouped_set - source_set),
            "duplicate_inclusion_count": len(grouped_ids) - len(grouped_set),
            "summary_index_mismatch_count": len(grouped_set ^ distinct_set),
            "summary_index_duplicate_count": len(distinct_ids) - len(distinct_set),
            "source_duplicate_count": len(source_ids) - len(source_set),
            "generation_diagnostic": generation_diagnostic,
            "narration_duration_ms": narration_duration_ms,
        }

    @staticmethod
    def _require_complete_activity_coverage(diagnostics: dict[str, Any]) -> None:
        invariant_fields = (
            "missing_source_activity_count",
            "unexpected_activity_count",
            "duplicate_inclusion_count",
            "summary_index_mismatch_count",
            "summary_index_duplicate_count",
            "source_duplicate_count",
        )
        if any(diagnostics[field] != 0 for field in invariant_fields):
            raise ReportValidationError(
                "report activity coverage invariant failed"
            )

    @staticmethod
    def _narration_payload(
        *,
        structured: dict[str, Any],
        source_digest: str,
    ) -> dict[str, Any]:
        bullets: list[dict[str, Any]] = []
        for section_name, items in structured["sections"].items():
            for item in items:
                bullets.append(
                    {
                        "bullet_id": item["bullet_id"],
                        "section": section_name,
                        "fact_ids": item["source_fact_ids"],
                        "template_text": item["text"],
                    }
                )
        return {
            "schema_version": REPORT_NARRATION_INPUT_VERSION,
            "source_digest": source_digest,
            "report_type": structured["report_type"],
            "period": structured["period"],
            "work_items": structured["work_items"],
            "current_exceptions": structured["current_exceptions"],
            "bullets": bullets,
        }

    def _validate_narration(
        self,
        *,
        candidate: dict[str, Any],
        payload: dict[str, Any],
        structured: dict[str, Any],
    ) -> dict[str, str]:
        if not isinstance(candidate, dict) or set(candidate) != {
            "schema_version",
            "source_digest",
            "bullets",
        }:
            raise ReportValidationError("narration envelope is invalid")
        if candidate["schema_version"] != REPORT_NARRATION_OUTPUT_VERSION:
            raise ReportValidationError("narration schema version is invalid")
        if candidate["source_digest"] != payload["source_digest"]:
            raise ReportValidationError("narration source digest changed")
        if not isinstance(candidate["bullets"], list):
            raise ReportValidationError("narration bullets must be a list")

        expected = payload["bullets"]
        if len(candidate["bullets"]) != len(expected):
            raise ReportValidationError("narration changed the bullet count")
        overrides: dict[str, str] = {}
        for expected_bullet, narrated_bullet in zip(expected, candidate["bullets"]):
            if not isinstance(narrated_bullet, dict) or set(narrated_bullet) != {
                "bullet_id",
                "fact_ids",
                "text",
            }:
                raise ReportValidationError("narration bullet is invalid")
            if narrated_bullet["bullet_id"] != expected_bullet["bullet_id"]:
                raise ReportValidationError("narration reordered or replaced a bullet")
            if narrated_bullet["fact_ids"] != expected_bullet["fact_ids"]:
                raise ReportValidationError("narration changed a bullet's facts")
            text = narrated_bullet["text"]
            if (
                not isinstance(text, str)
                or not text.strip()
                or len(text) > 1_000
                or "\n" in text
            ):
                raise ReportValidationError("narration text is invalid")
            text = text.strip()
            if not self._text_is_grounded(
                text=text,
                expected_bullet=expected_bullet,
                structured=structured,
            ):
                raise ReportValidationError("narration introduced an unknown fact")
            overrides[narrated_bullet["bullet_id"]] = text
        return overrides

    @classmethod
    def _text_is_grounded(
        cls,
        *,
        text: str,
        expected_bullet: dict[str, Any],
        structured: dict[str, Any],
    ) -> bool:
        work_item_id = expected_bullet["bullet_id"].split(":", 1)[-1]
        summary = next(
            (
                item
                for item in structured["work_items"]
                if item["work_item_id"] == work_item_id
            ),
            None,
        )
        exception = next(
            (
                item
                for item in structured["current_exceptions"]
                if item["work_item_id"] == work_item_id
            ),
            None,
        )
        values: list[str] = [expected_bullet["template_text"]]
        if summary:
            values.extend(
                value
                for value in (
                    summary["project_name"],
                    summary["work_item_title"],
                    summary["open_waiting"],
                    summary["open_blocked"],
                    summary["next_action"],
                )
                if value
            )
            values.extend(
                activity["summary"] for activity in summary["activity_groups"]
            )
        if exception:
            values.extend(
                value
                for value in (
                    exception["project_name"],
                    exception["title"],
                    exception["waiting_for"],
                    exception["blocked_reason"],
                    exception["next_action"],
                )
                if value
            )

        allowed_tokens = set()
        for value in values:
            allowed_tokens.update(cls._narration_tokens(value))
        allowed_tokens.update(cls._safe_narration_tokens())
        template_text = expected_bullet["template_text"]
        if cls._introduces_negation(text=text, template_text=template_text):
            return False
        if any(
            token not in allowed_tokens
            for token in cls._narration_tokens(text)
        ):
            return False

        assertion_markers = (
            "완료",
            "다시",
            "진행",
            "대기",
            "기다",
            "막",
            "회신",
            "요청",
            "보류",
        )
        if any(
            marker in text and marker not in template_text
            for marker in assertion_markers
        ):
            return False
        allowed_numbers = set(re.findall(r"\d+", " ".join(values)))
        if any(number not in allowed_numbers for number in re.findall(r"\d+", text)):
            return False
        return True

    @staticmethod
    def _introduces_negation(*, text: str, template_text: str) -> bool:
        """Reject a polarity change even when all positive fact tokens match.

        Korean short negators such as ``안`` and ``못`` were previously dropped by
        the two-character token filter. A narrator could therefore echo every
        grounded token while reversing the claim. Negation is allowed only when
        the deterministic template itself contains the same negation family.
        """

        patterns = (
            r"(?<![A-Za-z0-9가-힣])안(?![A-Za-z0-9가-힣])",
            r"안(?:됨|되|했|하|받|왔|진행|완료|처리|확인)",
            r"(?<![A-Za-z0-9가-힣])못(?:$|\s|[A-Za-z0-9가-힣])",
            r"않[가-힣]*",
            r"없[가-힣]*",
            r"아니[가-힣]*",
            r"(?<![A-Za-z0-9가-힣])미(?=\s)",
            r"미(?:완료|진행|확인|처리|해결|수신|응답|반영|작성|수정|시작)[가-힣]*",
            r"(?:불가|실패|취소|중단)[가-힣]*",
        )
        return any(
            re.search(pattern, text) is not None
            and re.search(pattern, template_text) is None
            for pattern in patterns
        )

    @staticmethod
    def _narration_tokens(value: str) -> set[str]:
        return {
            token.casefold()
            for token in re.findall(r"[A-Za-z0-9가-힣]+", value)
            if len(token) > 1
        }

    @staticmethod
    def _safe_narration_tokens() -> set[str]:
        return {
            "업무",
            "작업",
            "관련",
            "현재",
            "이번",
            "기간",
            "동안",
            "진행",
            "진행했습니다",
            "진행하고",
            "진행중입니다",
            "완료",
            "완료했습니다",
            "확인",
            "확인했습니다",
            "요청",
            "보냈습니다",
            "회신",
            "기다리고",
            "있습니다",
            "때문에",
            "막혀",
            "다음",
            "행동",
            "정리",
            "정리했습니다",
            "다시",
            "시작했습니다",
            "기록했습니다",
            "보류",
            "중입니다",
            "먼저",
            "하는",
            "좋겠습니다",
        }

    def _render_template(
        self,
        structured: dict[str, Any],
        *,
        text_overrides: dict[str, str] | None = None,
    ) -> str:
        labels = {
            "DAILY": "일일 업무 보고",
            "WEEKLY": "주간 업무 보고",
            "PROJECT": "프로젝트 업무 보고",
            "RANGE": "기간별 업무 보고",
        }
        period = structured["period"]
        lines = [
            f"{labels[structured['report_type']]} "
            f"({period['start_date']} ~ {period['end_date']}, "
            f"{period['timezone']})"
        ]
        if structured["project"]:
            lines.append(f"대상: {structured['project']['project_name']}")

        section_labels = (
            ("major_work", "주요 수행 업무"),
            ("completed_work", "완료 업무"),
            ("in_progress_work", "진행 중 업무"),
            ("issues", "이슈 / 대기사항"),
            ("next_actions", "다음 업무"),
            ("current_exceptions", "현재 예외 현황"),
        )
        has_period_facts = any(
            structured["sections"][name]
            for name in (
                "major_work",
                "completed_work",
                "in_progress_work",
                "issues",
                "next_actions",
            )
        )
        if not has_period_facts:
            lines.extend(["", "해당 기간에 기록된 업무가 없습니다."])
        for name, label in section_labels:
            items = structured["sections"][name]
            if not items:
                continue
            lines.extend(["", label])
            lines.extend(
                f"- {(text_overrides or {}).get(item['bullet_id'], item['text'])}"
                for item in items
            )
        return "\n".join(lines)

    def _current_exceptions(
        self,
        connection,
        *,
        user_id: str,
        project_id: str | None,
        touched_work_items: set[str],
        today: date,
        timezone_name: str,
    ) -> list[dict[str, Any]]:
        project_filter = ""
        parameters: list[Any] = [user_id]
        if project_id:
            project_filter = " AND p.id = ?"
            parameters.append(project_id)
        rows = connection.execute(
            f"""
            SELECT {self._snapshot_columns()}
            FROM work_items wi
            JOIN projects p
              ON p.id = wi.project_id
             AND p.user_id = wi.user_id
            WHERE wi.user_id = ?
              AND wi.archived_at IS NULL
              AND p.archived_at IS NULL
              AND wi.status IN ('WAITING', 'BLOCKED')
              {project_filter}
            ORDER BY p.name, wi.title, wi.id
            """,
            parameters,
        ).fetchall()
        result: list[dict[str, Any]] = []
        for row in rows:
            item = dict(row)
            if item["work_item_id"] in touched_work_items:
                continue
            if item["status"] == "BLOCKED" and item["next_action"]:
                result.append(item)
                continue
            if item["status"] != "WAITING":
                continue
            reference_day = (
                date.fromisoformat(item["last_activity_on"])
                if item["last_activity_on"]
                else self._utc_datetime(item["status_changed_at"])
                .astimezone(ZoneInfo(timezone_name))
                .date()
            )
            if (today - reference_day).days >= self.waiting_followup_days:
                result.append(item)
        return result

    def _source_manifest(
        self,
        *,
        activities: list[dict],
        audits: list[dict],
        work_items: list[dict],
    ) -> dict[str, list[dict[str, Any]]]:
        activity_sources: list[dict[str, Any]] = []
        link_sources: list[dict[str, Any]] = []
        project_sources: dict[str, dict[str, Any]] = {}
        for activity in activities:
            activity_core = {
                "id": activity["activity_id"],
                "version": activity["activity_version"],
                "validity": activity["validity"],
                "occurred_on_local": activity["occurred_on_local"],
                "kind": activity["kind"],
                "summary": activity["summary"],
            }
            activity_sources.append(
                {**activity_core, "digest": sha256_text(canonical_json(activity_core))}
            )
            link_core = {
                "id": activity["link_id"],
                "version": activity["link_version"],
                "activity_id": activity["activity_id"],
                "work_item_id": activity["work_item_id"],
                "is_active": 1,
            }
            link_sources.append(
                {**link_core, "digest": sha256_text(canonical_json(link_core))}
            )
            project_sources[activity["project_id"]] = {
                "id": activity["project_id"],
                "name": activity["project_name"],
            }

        audit_sources: list[dict[str, Any]] = []
        for audit in sorted(audits, key=lambda item: item["id"]):
            audit_core = {
                "id": audit["id"],
                "operation": audit["operation"],
                "target_type": audit["target_type"],
                "target_id": audit["target_id"],
                "expected_version": audit["expected_version"],
                "applied_version": audit["applied_version"],
                "before": audit["before"],
                "after": audit["after"],
                "correction_of_id": audit["correction_of_id"],
                "created_at": audit["created_at"],
            }
            audit_sources.append(
                {**audit_core, "digest": sha256_text(canonical_json(audit_core))}
            )

        work_item_sources: list[dict[str, Any]] = []
        for item in sorted(work_items, key=lambda value: value["work_item_id"]):
            core = {
                "id": item["work_item_id"],
                "project_id": item["project_id"],
                "title": item["title"],
                "status": item["status"],
                "priority": item["priority"],
                "waiting_for": item["waiting_for"],
                "blocked_reason": item["blocked_reason"],
                "next_action": item["next_action"],
                "version": item["version"],
            }
            work_item_sources.append(
                {**core, "digest": sha256_text(canonical_json(core))}
            )
            project_sources[item["project_id"]] = {
                "id": item["project_id"],
                "name": item["project_name"],
            }

        return {
            "activities": sorted(activity_sources, key=lambda item: item["id"]),
            "links": sorted(link_sources, key=lambda item: item["id"]),
            "audits": audit_sources,
            "work_items": work_item_sources,
            "projects": sorted(project_sources.values(), key=lambda item: item["id"]),
        }

    def _manifest_freshness(
        self,
        connection,
        *,
        user_id: str,
        manifest: dict,
    ) -> str:
        for source in manifest.get("activities", []):
            row = connection.execute(
                """
                SELECT id, version, validity, occurred_on_local, kind, summary
                FROM activities
                WHERE id = ? AND user_id = ?
                """,
                (source["id"], user_id),
            ).fetchone()
            if row is None:
                return "STALE"
            core = dict(row)
            if sha256_text(canonical_json(core)) != source["digest"]:
                return "STALE"

        for source in manifest.get("links", []):
            row = connection.execute(
                """
                SELECT id, version, activity_id, work_item_id, is_active
                FROM activity_links
                WHERE id = ? AND user_id = ?
                """,
                (source["id"], user_id),
            ).fetchone()
            if row is None:
                return "STALE"
            core = dict(row)
            if sha256_text(canonical_json(core)) != source["digest"]:
                return "STALE"

        audit_ids = {source["id"] for source in manifest.get("audits", [])}
        for source in manifest.get("audits", []):
            row = connection.execute(
                """
                SELECT id,
                       operation,
                       target_type,
                       target_id,
                       expected_version,
                       applied_version,
                       before_json,
                       after_json,
                       correction_of_id,
                       created_at
                FROM change_audit
                WHERE id = ? AND user_id = ?
                """,
                (source["id"], user_id),
            ).fetchone()
            if row is None:
                return "STALE"
            core = dict(row)
            core["before"] = self._json_object(core.pop("before_json"))
            core["after"] = self._json_object(core.pop("after_json"))
            if sha256_text(canonical_json(core)) != source["digest"]:
                return "STALE"
        if audit_ids:
            placeholders = ",".join("?" for _ in audit_ids)
            correction = connection.execute(
                f"""
                SELECT 1
                FROM change_audit
                WHERE user_id = ?
                  AND correction_of_id IN ({placeholders})
                LIMIT 1
                """,
                (user_id, *sorted(audit_ids)),
            ).fetchone()
            if correction:
                return "STALE"

        for source in manifest.get("work_items", []):
            row = connection.execute(
                """
                SELECT project_id
                FROM work_items
                WHERE id = ? AND user_id = ?
                """,
                (source["id"], user_id),
            ).fetchone()
            if row is None or row["project_id"] != source["project_id"]:
                return "STALE"
        return "FRESH"

    def _work_item_snapshots(
        self,
        connection,
        *,
        user_id: str,
        work_item_ids: set[str],
    ) -> dict[str, dict[str, Any]]:
        if not work_item_ids:
            return {}
        placeholders = ",".join("?" for _ in work_item_ids)
        rows = connection.execute(
            f"""
            SELECT {self._snapshot_columns()}
            FROM work_items wi
            JOIN projects p
              ON p.id = wi.project_id
             AND p.user_id = wi.user_id
            WHERE wi.user_id = ?
              AND wi.id IN ({placeholders})
            """,
            (user_id, *sorted(work_item_ids)),
        ).fetchall()
        result = {row["work_item_id"]: dict(row) for row in rows}
        missing = work_item_ids - result.keys()
        if missing:
            raise ReportValidationError("report source Work Item was not found")
        return result

    def _resolve_project(
        self,
        connection,
        *,
        user_id: str,
        project_mention: str | None,
        required: bool,
    ):
        if not project_mention:
            if required:
                raise ReportValidationError("PROJECT report requires project_mention")
            return None
        normalized = normalize_name(project_mention)
        row = connection.execute(
            """
            SELECT DISTINCT p.*
            FROM projects p
            LEFT JOIN project_aliases pa
              ON pa.project_id = p.id
             AND pa.user_id = p.user_id
            WHERE p.user_id = ?
              AND (
                    p.normalized_name = ?
                    OR pa.normalized_alias = ?
              )
            LIMIT 1
            """,
            (user_id, normalized, normalized),
        ).fetchone()
        if row is None:
            raise ReportValidationError(
                f"structured project was not found: {project_mention}"
            )
        return dict(row)

    def _resolve_period(
        self,
        *,
        report_type: ReportType,
        today: date,
        start_date: date | str | None,
        end_date: date | str | None,
        timezone_name: str,
        project: dict | None,
    ) -> tuple[date, date]:
        start = self._optional_date(start_date, field="start_date")
        end = self._optional_date(end_date, field="end_date")
        if report_type == ReportType.DAILY:
            if start and end and start != end:
                raise ReportValidationError("DAILY report accepts only one local date")
            target = start or end or today
            return target, target
        if report_type == ReportType.WEEKLY:
            if bool(start) != bool(end):
                raise ReportValidationError(
                    "WEEKLY explicit period requires both start_date and end_date"
                )
            if start and end:
                return self._validated_range(start, end)
            return today - timedelta(days=today.weekday()), today
        if report_type == ReportType.RANGE:
            if start is None or end is None:
                raise ReportValidationError(
                    "RANGE report requires start_date and end_date"
                )
            return self._validated_range(start, end)
        if bool(start) != bool(end):
            raise ReportValidationError(
                "PROJECT custom period requires both start_date and end_date"
            )
        if start and end:
            return self._validated_range(start, end)
        if project is None:
            raise ReportValidationError("PROJECT report requires a project")
        created = self._utc_datetime(project["created_at"])
        project_start = created.astimezone(ZoneInfo(timezone_name)).date()
        return project_start, today

    def _find_by_source_run(
        self,
        *,
        user_id: str,
        source_run_id: str,
    ) -> dict[str, Any] | None:
        connection = self.database.connect()
        try:
            row = connection.execute(
                """
                SELECT id
                FROM report_snapshots
                WHERE user_id = ? AND source_run_id = ?
                """,
                (user_id, source_run_id),
            ).fetchone()
        finally:
            connection.close()
        if row is None:
            return None
        return self.get_report(user_id=user_id, report_id=row["id"])

    def _require_source_run(self, *, user_id: str, source_run_id: str) -> None:
        connection = self.database.connect()
        try:
            row = connection.execute(
                """
                SELECT 1
                FROM orchestration_runs
                WHERE id = ? AND user_id = ?
                """,
                (source_run_id, user_id),
            ).fetchone()
            if row is None:
                raise ReportValidationError("source run was not found")
        finally:
            connection.close()

    @staticmethod
    def _snapshot_result(row) -> dict[str, Any]:
        structured = json.loads(row["structured_sections_json"])
        return {
            "report_id": row["id"],
            "report_type": row["report_type"],
            "period": structured["period"],
            "project": structured["project"],
            "sections": structured["sections"],
            "work_items": structured["work_items"],
            "current_exceptions": structured.get("current_exceptions", []),
            "rendered_text": row["rendered_text"],
            "source_digest": row["source_digest"],
            "freshness": row["freshness"],
            "generation_mode": row["generation_mode"],
            "generation_diagnostic": row["generation_diagnostic"],
            "narration_duration_ms": row["narration_duration_ms"],
            "diagnostics": json.loads(row["diagnostics_json"]),
            "policy_version": row["policy_version"],
            "as_of_utc": row["as_of_utc"],
        }

    @staticmethod
    def _outcome_codes(
        activities: list[dict],
        transitions: list[dict],
    ) -> list[str]:
        codes: list[str] = []
        status_pairs = [
            (
                transition["changes"].get("status", {}).get("before"),
                transition["changes"].get("status", {}).get("after"),
            )
            for transition in transitions
            if "status" in transition["changes"]
        ]
        if any(before != "DONE" and after == "DONE" for before, after in status_pairs):
            codes.append("COMPLETED")
        if any(before == "DONE" and after != "DONE" for before, after in status_pairs):
            codes.append("REOPENED")
        if any(before == "WAITING" and after != "WAITING" for before, after in status_pairs):
            codes.append("WAITING_RESOLVED")
        if any(before == "BLOCKED" and after != "BLOCKED" for before, after in status_pairs):
            codes.append("BLOCKED_RESOLVED")

        kinds = {activity["kind"] for activity in activities}
        kind_codes = (
            ("WORK_PERFORMED", "PROGRESSED"),
            ("RESPONSE_RECEIVED", "RESPONSE_RECEIVED"),
            ("REQUEST_SENT", "REQUESTED"),
            ("DECISION", "DECIDED"),
            ("NOTE", "NOTED"),
        )
        codes.extend(code for kind, code in kind_codes if kind in kinds)
        return codes

    @staticmethod
    def _major_text(summary: dict) -> str:
        project = summary["project_name"]
        title = summary["work_item_title"]
        outcomes = summary["outcome_codes"]
        if "COMPLETED" in outcomes:
            return f"{project}의 ‘{title}’ 업무를 완료했습니다."
        if "REOPENED" in outcomes:
            return f"{project}의 ‘{title}’ 업무를 다시 진행하기 시작했습니다."
        if "PROGRESSED" in outcomes:
            return f"{project}의 ‘{title}’ 업무를 진행했습니다."
        if "RESPONSE_RECEIVED" in outcomes:
            return f"{project}의 ‘{title}’ 업무와 관련된 회신을 확인했습니다."
        if "REQUESTED" in outcomes:
            return f"{project}의 ‘{title}’ 업무와 관련된 요청을 보냈습니다."
        if "DECIDED" in outcomes:
            return f"{project}의 ‘{title}’ 업무와 관련된 결정을 기록했습니다."
        return f"{project}의 ‘{title}’ 업무 상태를 정리했습니다."

    @classmethod
    def _issue_text(cls, summary: dict) -> str | None:
        if summary["open_waiting"]:
            return (
                f"{summary['project_name']}의 ‘{summary['work_item_title']}’ 업무는 "
                f"현재 기다리는 내용이 ‘{summary['open_waiting']}’입니다."
            )
        if summary["open_blocked"]:
            return (
                f"{summary['project_name']}의 ‘{summary['work_item_title']}’ 업무는 "
                f"‘{summary['open_blocked']}’ 때문에 막혀 있습니다."
            )
        if summary["current_status"] == "HOLD":
            return (
                f"{summary['project_name']}의 "
                f"‘{summary['work_item_title']}’ 업무는 보류 중입니다."
            )
        return None

    @staticmethod
    def _snapshot_issue_text(item: dict) -> str | None:
        if item["status"] == "WAITING" and item["waiting_for"]:
            return (
                f"{item['project_name']}의 ‘{item['title']}’ 업무는 "
                f"‘{item['waiting_for']}’ 관련 상황을 확인할 시점입니다."
            )
        if item["status"] == "BLOCKED" and item["blocked_reason"]:
            return (
                f"{item['project_name']}의 ‘{item['title']}’ 업무는 "
                f"‘{item['blocked_reason']}’ 때문에 막혀 있습니다."
            )
        return None

    @staticmethod
    def _section_item(
        section_name: str,
        summary: dict,
        text: str,
        refs: list[str],
    ) -> dict:
        return {
            "bullet_id": f"{section_name}:{summary['work_item_id']}",
            "project_id": summary["project_id"],
            "work_item_id": summary["work_item_id"],
            "text": text,
            "source_fact_ids": refs,
        }

    @classmethod
    def _audit_changes_report_fact(cls, audit: dict) -> bool:
        before = audit["before"] or {}
        after = audit["after"] or {}
        return any(
            before.get(field) != after.get(field)
            for field in cls._tracked_audit_fields()
        )

    @staticmethod
    def _tracked_audit_fields() -> tuple[str, ...]:
        return (
            "project_id",
            "title",
            "status",
            "priority",
            "waiting_for",
            "blocked_reason",
            "next_action",
            "completed_at",
            "archived_at",
        )

    @staticmethod
    def _status_after(audit: dict | None) -> str | None:
        if audit is None:
            return None
        return (audit["after"] or {}).get("status")

    @staticmethod
    def _normalize_activity_summary(value: str) -> str:
        compact = re.sub(r"\s+", "", value.casefold())
        return re.sub(r"[^a-z0-9가-힣]", "", compact)

    @staticmethod
    def _user_timezone(connection, user_id: str) -> str:
        row = connection.execute(
            "SELECT timezone FROM users WHERE id = ?",
            (user_id,),
        ).fetchone()
        if row is None:
            raise ReportValidationError("user was not found")
        try:
            ZoneInfo(row["timezone"])
        except Exception as exc:
            raise ReportValidationError("user timezone is invalid") from exc
        return row["timezone"]

    @staticmethod
    def _utc_bounds(
        start_date: date,
        end_date: date,
        timezone_name: str,
    ) -> tuple[datetime, datetime]:
        zone = ZoneInfo(timezone_name)
        start = datetime.combine(start_date, time.min, tzinfo=zone)
        end_exclusive = datetime.combine(
            end_date + timedelta(days=1),
            time.min,
            tzinfo=zone,
        )
        return start.astimezone(timezone.utc), end_exclusive.astimezone(timezone.utc)

    @staticmethod
    def _utc_datetime(value: str) -> datetime:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            raise ReportValidationError("UTC timestamp must include timezone")
        return parsed.astimezone(timezone.utc)

    @staticmethod
    def _json_object(value: str | None) -> dict | None:
        if value is None:
            return None
        parsed = json.loads(value)
        if not isinstance(parsed, dict):
            raise ReportValidationError("audit payload must be a JSON object")
        return parsed

    @staticmethod
    def _report_type(value: str | ReportType) -> ReportType:
        try:
            return ReportType(str(value).upper())
        except ValueError as exc:
            raise ReportValidationError(
                "report_type must be DAILY, WEEKLY, PROJECT, or RANGE"
            ) from exc

    @classmethod
    def _optional_date(
        cls,
        value: date | str | None,
        *,
        field: str,
    ) -> date | None:
        return None if value is None else cls._date_value(value, field=field)

    @staticmethod
    def _date_value(value: date | str, *, field: str) -> date:
        if isinstance(value, datetime):
            raise ReportValidationError(f"{field} must be a local date")
        if isinstance(value, date):
            return value
        try:
            return date.fromisoformat(value)
        except (TypeError, ValueError) as exc:
            raise ReportValidationError(f"{field} must be an ISO local date") from exc

    @staticmethod
    def _validated_range(start: date, end: date) -> tuple[date, date]:
        if start > end:
            raise ReportValidationError("start_date must not be after end_date")
        return start, end

    @staticmethod
    def _snapshot_columns() -> str:
        return """
            wi.id AS work_item_id,
            wi.project_id,
            p.name AS project_name,
            wi.title,
            wi.status,
            wi.priority,
            wi.waiting_for,
            wi.blocked_reason,
            wi.next_action,
            wi.version,
            wi.status_changed_at,
            wi.last_activity_on,
            wi.completed_at,
            wi.archived_at
        """
