from __future__ import annotations

import json
from collections import defaultdict
from datetime import date, datetime, time, timedelta, timezone
from math import ceil
from typing import Any, Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import BaseModel, ConfigDict, Field

from app.database import Database
from app.stabilization import CORRECTION_EVENT_TYPE
from app.utils import sha256_text, utc_iso


VALIDATION_SUMMARY_SCHEMA_VERSION = "validation-summary.v1"


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ValidationPeriod(_StrictModel):
    start_local: date
    end_local: date
    timezone: str
    coverage: Literal["COMPLETE", "PARTIAL"]


class CountMetric(_StrictModel):
    count: int = Field(ge=0)


class RateMetric(_StrictModel):
    numerator: int = Field(ge=0)
    denominator: int = Field(ge=0)
    value: float | None = Field(default=None, ge=0, le=1)


class ExtractionMetric(_StrictModel):
    started_attempts: int = Field(ge=0)
    completed_attempts: int = Field(ge=0)
    failures: int = Field(ge=0)
    final_failed_requests: int = Field(ge=0)
    recovered_attempts: int = Field(ge=0)


class LatencyMetric(_StrictModel):
    samples: int = Field(ge=0)
    average: float | None = Field(default=None, ge=0)
    p95: int | None = Field(default=None, ge=0)


class ProviderTimeoutMetric(_StrictModel):
    total: int = Field(ge=0)
    extraction: int = Field(ge=0)
    report_narration: int = Field(ge=0)
    recommendation_narration: int = Field(ge=0)


class ValidationMetrics(_StrictModel):
    context_auto_link_successes: CountMetric
    context_clarifications: CountMetric
    context_corrections: CountMetric
    project_misclassifications: CountMetric
    work_item_duplicate_candidates: CountMetric
    potential_duplicate_activities: CountMetric
    incorrect_status_changes: CountMetric
    incorrect_next_actions: CountMetric
    report_corrections_required: CountMetric
    llm_extraction: ExtractionMetric
    ollama_latency_ms: LatencyMetric
    provider_timeouts: ProviderTimeoutMetric


class ValidationRates(_StrictModel):
    context_correction_rate: RateMetric
    clarification_rate: RateMetric
    extraction_failure_rate: RateMetric
    report_correction_rate: RateMetric
    provider_timeout_rate: RateMetric
    extraction_timeout_rate: RateMetric


class ValidationSupplemental(_StrictModel):
    context_auto_link_decisions: int = Field(ge=0)
    context_link_opportunities: int = Field(ge=0)
    reports_generated: int = Field(ge=0)
    report_narration_attempts: int = Field(ge=0)
    recommendation_narration_attempts: int = Field(ge=0)
    work_items_created: int = Field(ge=0)
    activities_recorded: int = Field(ge=0)
    report_structural_anomalies: int = Field(ge=0)
    report_stale_snapshots: int = Field(ge=0)
    report_template_fallbacks: int = Field(ge=0)
    report_narration_latency_ms: LatencyMetric


class ValidationMethodology(_StrictModel):
    period_interval: Literal["LOCAL_DATES_INCLUSIVE_UTC_HALF_OPEN"]
    zero_denominator_rate: Literal["NOT_AVAILABLE"]
    auto_link_success_is_observed_acceptance: Literal[True]
    semantic_errors_require_explicit_findings: Literal[True]


class ValidationCompleteness(_StrictModel):
    status: Literal["PASS", "PARTIAL"]
    malformed_decision_count: int = Field(ge=0)
    malformed_event_payload_count: int = Field(ge=0)
    incomplete_extraction_attempt_count: int = Field(ge=0)
    missing_local_duration_count: int = Field(ge=0)
    unmatched_context_correction_count: int = Field(ge=0)
    coarse_context_correction_match_count: int = Field(ge=0)
    unmatched_report_correction_count: int = Field(ge=0)
    malformed_report_diagnostics_count: int = Field(ge=0)
    malformed_result_payload_count: int = Field(ge=0)


class ValidationSummary(_StrictModel):
    schema_version: Literal["validation-summary.v1"]
    generated_at: datetime
    period: ValidationPeriod
    metrics: ValidationMetrics
    rates: ValidationRates
    supplemental: ValidationSupplemental
    completeness: ValidationCompleteness
    methodology: ValidationMethodology


class ValidationPeriodError(ValueError):
    pass


class ValidationSummaryService:
    """Build a privacy-safe, read-only validation summary.

    Only aggregate counts and timing values leave this service. Message text,
    entity names, IDs, hashes, provider responses, and arbitrary event payloads
    are deliberately absent from the output schema.
    """

    _CONTEXT_DECISIONS = frozenset(
        {"AUTO_LINK", "NEEDS_CLARIFICATION", "UNRESOLVED"}
    )
    _REPORT_INVARIANT_FIELDS = (
        "missing_source_activity_count",
        "unexpected_activity_count",
        "duplicate_inclusion_count",
        "summary_index_mismatch_count",
        "summary_index_duplicate_count",
        "source_duplicate_count",
    )

    def __init__(self, database: Database) -> None:
        self.database = database

    def summarize(
        self,
        *,
        user_id: str = "local-user",
        week_containing: date | None = None,
        start_local: date | None = None,
        end_local: date | None = None,
    ) -> ValidationSummary:
        timezone_name = self._timezone_for_user(user_id)
        try:
            local_zone = ZoneInfo(timezone_name)
        except ZoneInfoNotFoundError as exc:
            raise ValidationPeriodError("stored user timezone is invalid") from exc

        now_utc = self.database.clock.now_utc().astimezone(timezone.utc)
        resolved_start, resolved_end = self._resolve_period(
            now_utc=now_utc,
            local_zone=local_zone,
            week_containing=week_containing,
            start_local=start_local,
            end_local=end_local,
        )
        start_utc = datetime.combine(
            resolved_start, time.min, tzinfo=local_zone
        ).astimezone(timezone.utc)
        end_exclusive_utc = datetime.combine(
            resolved_end + timedelta(days=1), time.min, tzinfo=local_zone
        ).astimezone(timezone.utc)
        if start_utc >= now_utc:
            raise ValidationPeriodError("validation period is wholly in the future")
        # Future-dated rows must not enter a summary "as of" its generation.
        cohort_end_utc = min(end_exclusive_utc, now_utc)
        start_iso = utc_iso(start_utc)
        end_iso = utc_iso(cohort_end_utc)
        as_of_iso = utc_iso(now_utc)

        connection = self.database.connect()
        try:
            connection.execute("PRAGMA query_only = ON")
            findings = self._load_findings(
                connection,
                user_id=user_id,
                as_of_iso=as_of_iso,
            )
            context = self._context_metrics(
                connection,
                user_id=user_id,
                start_iso=start_iso,
                end_iso=end_iso,
                as_of_iso=as_of_iso,
                findings=findings,
            )
            duplicates = self._duplicate_metrics(
                connection,
                user_id=user_id,
                start_iso=start_iso,
                end_iso=end_iso,
            )
            extraction = self._extraction_metrics(
                connection,
                user_id=user_id,
                start_iso=start_iso,
                end_iso=end_iso,
                as_of_iso=as_of_iso,
            )
            reports = self._report_metrics(
                connection,
                user_id=user_id,
                start_iso=start_iso,
                end_iso=end_iso,
                as_of_iso=as_of_iso,
                findings=findings,
            )
            recommendation = self._recommendation_metrics(
                connection,
                user_id=user_id,
                start_iso=start_iso,
                end_iso=end_iso,
            )
        finally:
            connection.close()

        project_misclassifications = (
            context["native_project_misclassifications"]
            + self._finding_count(
                findings,
                category="PROJECT_MISCLASSIFICATION",
                start_iso=start_iso,
                end_iso=end_iso,
            )
        )
        context_corrections = (
            context["corrections"]
            + self._user_context_finding_count(
                findings, start_iso=start_iso, end_iso=end_iso
            )
        )
        status_errors = self._finding_count(
            findings,
            category="STATUS_INCORRECT",
            start_iso=start_iso,
            end_iso=end_iso,
        )
        next_action_errors = self._finding_count(
            findings,
            category="NEXT_ACTION_INCORRECT",
            start_iso=start_iso,
            end_iso=end_iso,
        )
        report_corrections = self._finding_count(
            findings,
            category="REPORT_CORRECTION_REQUIRED",
            start_iso=start_iso,
            end_iso=end_iso,
        )
        provider_timeout_total = (
            extraction["timeouts"]
            + reports["narrator_timeouts"]
            + recommendation["timeouts"]
        )
        provider_calls = (
            extraction["started_attempts"]
            + reports["narrator_attempts"]
            + recommendation["attempts"]
        )

        return ValidationSummary(
            schema_version=VALIDATION_SUMMARY_SCHEMA_VERSION,
            generated_at=now_utc,
            period=ValidationPeriod(
                start_local=resolved_start,
                end_local=resolved_end,
                timezone=timezone_name,
                coverage=(
                    "COMPLETE" if end_exclusive_utc <= now_utc else "PARTIAL"
                ),
            ),
            metrics=ValidationMetrics(
                context_auto_link_successes=CountMetric(
                    count=context["auto_link_successes"]
                ),
                context_clarifications=CountMetric(
                    count=context["clarifications"]
                ),
                context_corrections=CountMetric(
                    count=context_corrections
                ),
                project_misclassifications=CountMetric(
                    count=project_misclassifications
                ),
                work_item_duplicate_candidates=CountMetric(
                    count=duplicates["work_item_candidates"]
                ),
                potential_duplicate_activities=CountMetric(
                    count=duplicates["activity_candidates"]
                ),
                incorrect_status_changes=CountMetric(count=status_errors),
                incorrect_next_actions=CountMetric(count=next_action_errors),
                report_corrections_required=CountMetric(
                    count=report_corrections
                ),
                llm_extraction=ExtractionMetric(
                    started_attempts=extraction["started_attempts"],
                    completed_attempts=extraction["completed_attempts"],
                    failures=extraction["failures"],
                    final_failed_requests=extraction["final_failed_requests"],
                    recovered_attempts=extraction["recovered_attempts"],
                ),
                ollama_latency_ms=self._latency(extraction["ollama_latencies"]),
                provider_timeouts=ProviderTimeoutMetric(
                    total=provider_timeout_total,
                    extraction=extraction["timeouts"],
                    report_narration=reports["narrator_timeouts"],
                    recommendation_narration=recommendation["timeouts"],
                ),
            ),
            rates=ValidationRates(
                context_correction_rate=self._rate(
                    context["auto_link_corrections"],
                    context["applied_auto_link_decisions"],
                ),
                clarification_rate=self._rate(
                    context["clarifications"],
                    context["opportunities"],
                ),
                extraction_failure_rate=self._rate(
                    extraction["failures"], extraction["completed_attempts"]
                ),
                report_correction_rate=self._rate(
                    reports["corrected_cohort"], reports["generated"]
                ),
                provider_timeout_rate=self._rate(
                    provider_timeout_total, provider_calls
                ),
                extraction_timeout_rate=self._rate(
                    extraction["timeouts"], extraction["started_attempts"]
                ),
            ),
            supplemental=ValidationSupplemental(
                context_auto_link_decisions=context["auto_link_decisions"],
                context_link_opportunities=context["opportunities"],
                reports_generated=reports["generated"],
                report_narration_attempts=reports["narrator_attempts"],
                recommendation_narration_attempts=recommendation["attempts"],
                work_items_created=duplicates["work_items_created"],
                activities_recorded=duplicates["activities_recorded"],
                report_structural_anomalies=reports["structural_anomalies"],
                report_stale_snapshots=reports["stale"],
                report_template_fallbacks=reports["template_fallbacks"],
                report_narration_latency_ms=self._latency(
                    reports["narration_latencies"]
                ),
            ),
            completeness=self._completeness(
                malformed_decision_count=context["malformed_decisions"],
                malformed_event_payload_count=(
                    context["malformed_event_payloads"]
                    + extraction["malformed_event_payloads"]
                ),
                incomplete_extraction_attempt_count=extraction[
                    "incomplete_attempts"
                ],
                missing_local_duration_count=extraction[
                    "missing_local_durations"
                ],
                unmatched_context_correction_count=context[
                    "unmatched_corrections"
                ],
                coarse_context_correction_match_count=context[
                    "coarse_correction_matches"
                ],
                unmatched_report_correction_count=reports[
                    "unmatched_corrections"
                ],
                malformed_report_diagnostics_count=reports[
                    "malformed_diagnostics"
                ],
                malformed_result_payload_count=recommendation[
                    "malformed_results"
                ],
            ),
            methodology=ValidationMethodology(
                period_interval="LOCAL_DATES_INCLUSIVE_UTC_HALF_OPEN",
                zero_denominator_rate="NOT_AVAILABLE",
                auto_link_success_is_observed_acceptance=True,
                semantic_errors_require_explicit_findings=True,
            ),
        )

    def _timezone_for_user(self, user_id: str) -> str:
        connection = self.database.connect()
        try:
            connection.execute("PRAGMA query_only = ON")
            row = connection.execute(
                "SELECT timezone FROM users WHERE id = ?", (user_id,)
            ).fetchone()
        finally:
            connection.close()
        if row is None:
            raise ValidationPeriodError("user does not exist")
        return str(row["timezone"])

    @staticmethod
    def _resolve_period(
        *,
        now_utc: datetime,
        local_zone: ZoneInfo,
        week_containing: date | None,
        start_local: date | None,
        end_local: date | None,
    ) -> tuple[date, date]:
        explicit_range = start_local is not None or end_local is not None
        if week_containing is not None and explicit_range:
            raise ValidationPeriodError(
                "week_containing cannot be combined with an explicit range"
            )
        if explicit_range and (start_local is None or end_local is None):
            raise ValidationPeriodError(
                "start_local and end_local must be provided together"
            )
        if explicit_range:
            assert start_local is not None and end_local is not None
            if end_local < start_local:
                raise ValidationPeriodError(
                    "end_local must be on or after start_local"
                )
            return start_local, end_local

        anchor = week_containing or now_utc.astimezone(local_zone).date()
        week_start = anchor - timedelta(days=anchor.weekday())
        return week_start, week_start + timedelta(days=6)

    @classmethod
    def _context_metrics(
        cls,
        connection,
        *,
        user_id: str,
        start_iso: str,
        end_iso: str,
        as_of_iso: str,
        findings: list[dict[str, str]],
    ) -> dict[str, int]:
        decisions: list[dict[str, Any]] = []
        malformed_decisions = 0
        rows = connection.execute(
            """
            SELECT wfg.id, wfg.run_id, wfg.status, wfg.decision_json,
                   wfg.created_at,
                   EXISTS (
                     SELECT 1 FROM change_receipts cr
                     WHERE cr.user_id = wfg.user_id
                       AND cr.fact_group_id = wfg.id
                   ) AS has_receipt
            FROM work_fact_groups wfg
            WHERE wfg.user_id = ? AND wfg.created_at < ?
            ORDER BY created_at, group_sequence
            """,
            (user_id, as_of_iso),
        ).fetchall()
        all_fact_ids = {str(row["id"]) for row in rows}
        fact_id_by_hash = {
            sha256_text(str(row["id"])): str(row["id"]) for row in rows
        }
        fact_ids_by_run: defaultdict[str, list[str]] = defaultdict(list)
        for row in rows:
            fact_ids_by_run[str(row["run_id"])].append(str(row["id"]))
        fact_id_by_unique_run_hash = {
            sha256_text(run_id): ids[0]
            for run_id, ids in fact_ids_by_run.items()
            if len(ids) == 1
        }
        for row in rows:
            try:
                decision = json.loads(row["decision_json"] or "{}").get("decision")
            except (TypeError, json.JSONDecodeError):
                if start_iso <= str(row["created_at"]) < end_iso:
                    malformed_decisions += 1
                continue
            if decision not in {
                "AUTO_LINK",
                "CREATE_NEW",
                "NEEDS_CLARIFICATION",
                "UNRESOLVED",
            }:
                if start_iso <= str(row["created_at"]) < end_iso:
                    malformed_decisions += 1
                continue
            decisions.append(
                {
                    "id": str(row["id"]),
                    "run_id": str(row["run_id"]),
                    "status": str(row["status"]),
                    "decision": decision,
                    "created_at": str(row["created_at"]),
                    "has_receipt": bool(row["has_receipt"]),
                }
            )

        cohort = [
            item
            for item in decisions
            if start_iso <= item["created_at"] < end_iso
        ]
        context_decisions = [
            item for item in cohort if item["decision"] in cls._CONTEXT_DECISIONS
        ]
        all_auto_links = [
            item for item in decisions if item["decision"] == "AUTO_LINK"
        ]
        all_auto_ids = {item["id"] for item in all_auto_links}
        decision_by_id = {item["id"]: item["decision"] for item in decisions}
        cohort_auto_links = [
            item for item in cohort if item["decision"] == "AUTO_LINK"
        ]
        applied_auto_links = [
            item
            for item in cohort_auto_links
            if item["status"] == "APPLIED" and item["has_receipt"]
        ]
        corrected_fact_groups: set[str] = set()
        correction_occurrences = 0
        native_project_occurrences = 0
        malformed_event_payloads = 0
        unmatched_corrections = 0
        correction_rows = connection.execute(
            """
            SELECT payload_json, created_at
            FROM execution_events
            WHERE user_id = ?
              AND event_type = ?
              AND created_at < ?
            ORDER BY created_at, sequence
            """,
            (user_id, CORRECTION_EVENT_TYPE, as_of_iso),
        ).fetchall()
        for row in correction_rows:
            try:
                payload = json.loads(row["payload_json"] or "{}")
            except (TypeError, json.JSONDecodeError):
                if start_iso <= str(row["created_at"]) < end_iso:
                    malformed_event_payloads += 1
                continue
            case_id = payload.get("case_id")
            observed = payload.get("observed_link") or {}
            fact_group_id = observed.get("fact_group_id")
            matched_fact_group = (
                isinstance(fact_group_id, str) and fact_group_id in all_fact_ids
            )
            matched_auto_link = (
                matched_fact_group and fact_group_id in all_auto_ids
            )
            if matched_auto_link:
                corrected_fact_groups.add(fact_group_id)
            created_at = str(row["created_at"])
            if not isinstance(case_id, str):
                if start_iso <= created_at < end_iso:
                    malformed_event_payloads += 1
                continue
            if start_iso <= created_at < end_iso:
                correction_occurrences += 1
                if payload.get("category") == "PROJECT_MISCLASSIFICATION":
                    native_project_occurrences += 1
                if not matched_fact_group:
                    unmatched_corrections += 1

        applied_auto_ids = {item["id"] for item in applied_auto_links}
        cohort_fact_ids = {item["id"] for item in cohort}
        coarse_correction_matches = 0
        for finding in findings:
            if finding["source_type"] == "CONTEXT_CORRECTION_EVENT" or finding[
                "category"
            ] not in {"CONTEXT_LINK_INCORRECT", "PROJECT_MISCLASSIFICATION"}:
                continue
            matched_id = fact_id_by_hash.get(finding["source_ref_hash"])
            coarse = False
            if matched_id is None:
                matched_id = fact_id_by_unique_run_hash.get(
                    finding["source_ref_hash"]
                )
                coarse = matched_id is not None
            if matched_id is not None:
                if decision_by_id.get(matched_id) == "AUTO_LINK":
                    corrected_fact_groups.add(matched_id)
                if (
                    coarse
                    and (
                        matched_id in cohort_fact_ids
                        or start_iso <= finding["recorded_at"] < end_iso
                    )
                ):
                    coarse_correction_matches += 1
            elif start_iso <= finding["recorded_at"] < end_iso:
                unmatched_corrections += 1

        auto_link_successes = sum(
            1 for item in applied_auto_links if item["id"] not in corrected_fact_groups
        )
        clarifications = int(
            connection.execute(
                """
                SELECT COUNT(DISTINCT fact_group_id)
                FROM clarifications
                WHERE user_id = ? AND created_at >= ? AND created_at < ?
                """,
                (user_id, start_iso, end_iso),
            ).fetchone()[0]
        )
        return {
            "auto_link_decisions": len(cohort_auto_links),
            "applied_auto_link_decisions": len(applied_auto_links),
            "auto_link_successes": auto_link_successes,
            "auto_link_corrections": len(
                applied_auto_ids & corrected_fact_groups
            ),
            "opportunities": len(context_decisions),
            "clarifications": clarifications,
            "corrections": correction_occurrences,
            "native_project_misclassifications": native_project_occurrences,
            "malformed_decisions": malformed_decisions,
            "malformed_event_payloads": malformed_event_payloads,
            "unmatched_corrections": unmatched_corrections,
            "coarse_correction_matches": coarse_correction_matches,
        }

    @staticmethod
    def _load_findings(
        connection,
        *,
        user_id: str,
        as_of_iso: str,
    ) -> list[dict[str, str]]:
        table = connection.execute(
            """
            SELECT 1
            FROM sqlite_master
            WHERE type = 'table' AND name = 'validation_findings'
            """
        ).fetchone()
        if table is None:
            raise ValidationPeriodError(
                "validation findings schema is not initialized"
            )
        rows = connection.execute(
            """
            SELECT category, source_type, source_ref_hash, recorded_at
            FROM validation_findings
            WHERE user_id = ?
              AND recorded_at < ?
            ORDER BY recorded_at, id
            """,
            (user_id, as_of_iso),
        ).fetchall()
        return [
            {
                "category": str(row["category"]),
                "source_type": str(row["source_type"]),
                "source_ref_hash": str(row["source_ref_hash"]),
                "recorded_at": str(row["recorded_at"]),
            }
            for row in rows
        ]

    @staticmethod
    def _finding_count(
        findings: list[dict[str, str]],
        *,
        category: str,
        start_iso: str,
        end_iso: str,
    ) -> int:
        return sum(
            1
            for finding in findings
            if finding["category"] == category
            and finding["source_type"] != "CONTEXT_CORRECTION_EVENT"
            and start_iso <= finding["recorded_at"] < end_iso
        )

    @staticmethod
    def _user_context_finding_count(
        findings: list[dict[str, str]],
        *,
        start_iso: str,
        end_iso: str,
    ) -> int:
        return sum(
            1
            for finding in findings
            if finding["source_type"] == "USER_CORRECTION"
            and finding["category"]
            in {"CONTEXT_LINK_INCORRECT", "PROJECT_MISCLASSIFICATION"}
            and start_iso <= finding["recorded_at"] < end_iso
        )

    @staticmethod
    def _duplicate_metrics(
        connection,
        *,
        user_id: str,
        start_iso: str,
        end_iso: str,
    ) -> dict[str, int]:
        work_items_created = int(
            connection.execute(
                """
                SELECT COUNT(*)
                FROM work_items
                WHERE user_id = ?
                  AND archived_at IS NULL
                  AND created_at >= ? AND created_at < ?
                """,
                (user_id, start_iso, end_iso),
            ).fetchone()[0]
        )
        work_item_candidates = int(
            connection.execute(
                """
                SELECT COUNT(*)
                FROM work_items current
                WHERE current.user_id = ?
                  AND current.archived_at IS NULL
                  AND current.created_at >= ?
                  AND current.created_at < ?
                  AND EXISTS (
                    SELECT 1
                    FROM work_items prior
                    WHERE prior.user_id = current.user_id
                      AND prior.archived_at IS NULL
                      AND prior.project_id = current.project_id
                      AND prior.normalized_title = current.normalized_title
                      AND (
                        prior.created_at < current.created_at
                        OR (
                          prior.created_at = current.created_at
                          AND prior.id < current.id
                        )
                      )
                  )
                """,
                (user_id, start_iso, end_iso),
            ).fetchone()[0]
        )
        activities_recorded = int(
            connection.execute(
                """
                SELECT COUNT(*)
                FROM activities
                WHERE user_id = ?
                  AND validity = 'ACTIVE'
                  AND recorded_at_utc >= ?
                  AND recorded_at_utc < ?
                """,
                (user_id, start_iso, end_iso),
            ).fetchone()[0]
        )
        activity_candidates = int(
            connection.execute(
                """
                SELECT COUNT(*)
                FROM activities current
                WHERE current.user_id = ?
                  AND current.validity = 'ACTIVE'
                  AND current.recorded_at_utc >= ?
                  AND current.recorded_at_utc < ?
                  AND EXISTS (
                    SELECT 1
                    FROM activities prior
                    WHERE prior.user_id = current.user_id
                      AND prior.validity = 'ACTIVE'
                      AND prior.source_excerpt_hash = current.source_excerpt_hash
                      AND prior.kind = current.kind
                      AND prior.occurred_on_local = current.occurred_on_local
                      AND (
                        prior.recorded_at_utc < current.recorded_at_utc
                        OR (
                          prior.recorded_at_utc = current.recorded_at_utc
                          AND prior.id < current.id
                        )
                      )
                  )
                """,
                (user_id, start_iso, end_iso),
            ).fetchone()[0]
        )
        return {
            "work_items_created": work_items_created,
            "work_item_candidates": work_item_candidates,
            "activities_recorded": activities_recorded,
            "activity_candidates": activity_candidates,
        }

    @classmethod
    def _extraction_metrics(
        cls,
        connection,
        *,
        user_id: str,
        start_iso: str,
        end_iso: str,
        as_of_iso: str,
    ) -> dict[str, Any]:
        rows = connection.execute(
            """
            SELECT e.run_id, e.sequence, e.event_type, e.payload_json,
                   e.created_at, r.status, r.provider_name
            FROM execution_events e
            JOIN orchestration_runs r
              ON r.id = e.run_id AND r.user_id = e.user_id
            WHERE e.user_id = ? AND e.created_at < ?
            ORDER BY e.run_id, e.sequence
            """,
            (user_id, as_of_iso),
        ).fetchall()
        events_by_run: defaultdict[str, list[Any]] = defaultdict(list)
        for row in rows:
            events_by_run[str(row["run_id"])].append(row)

        attempts: list[dict[str, Any]] = []
        malformed_event_keys: set[tuple[str, int]] = set()
        for row in rows:
            if row["event_type"] not in {
                "INTERPRETATION_STARTED",
                "INTERPRETATION_COMPLETED",
                "REQUEST_FAILED",
            } or not (start_iso <= str(row["created_at"]) < end_iso):
                continue
            try:
                payload = json.loads(row["payload_json"] or "{}")
                if not isinstance(payload, dict):
                    raise TypeError
            except (TypeError, json.JSONDecodeError):
                malformed_event_keys.add(
                    (str(row["run_id"]), int(row["sequence"]))
                )
        for run_rows in events_by_run.values():
            for index, row in enumerate(run_rows):
                if row["event_type"] != "INTERPRETATION_STARTED":
                    continue
                in_cohort = start_iso <= str(row["created_at"]) < end_iso
                try:
                    started_payload = json.loads(row["payload_json"] or "{}")
                    if not isinstance(started_payload, dict):
                        raise TypeError
                except (TypeError, json.JSONDecodeError):
                    started_payload = {}
                    if in_cohort:
                        malformed_event_keys.add(
                            (str(row["run_id"]), int(row["sequence"]))
                        )
                provider = started_payload.get("provider") or row["provider_name"]
                if provider not in {"local", "api"}:
                    continue

                attempt: dict[str, Any] = {
                    "run_id": str(row["run_id"]),
                    "sequence": int(row["sequence"]),
                    "provider": provider,
                    "in_cohort": in_cohort,
                    "failure": False,
                    "timeout": False,
                    "latency": None,
                    "interpretation_completed": False,
                    "terminal": False,
                    "final_status": str(row["status"]),
                }
                for outcome in run_rows[index + 1 :]:
                    if outcome["event_type"] == "INTERPRETATION_STARTED":
                        break
                    if outcome["event_type"] == "INTERPRETATION_COMPLETED":
                        attempt["interpretation_completed"] = True
                        try:
                            payload = json.loads(outcome["payload_json"] or "{}")
                            if not isinstance(payload, dict):
                                raise TypeError
                        except (TypeError, json.JSONDecodeError):
                            payload = {}
                            if in_cohort:
                                malformed_event_keys.add(
                                    (
                                        str(outcome["run_id"]),
                                        int(outcome["sequence"]),
                                    )
                                )
                        duration = payload.get("duration_ms")
                        if isinstance(duration, int) and duration >= 0:
                            attempt["latency"] = duration
                    elif outcome["event_type"] == "REQUEST_FAILED":
                        try:
                            payload = json.loads(outcome["payload_json"] or "{}")
                            if not isinstance(payload, dict):
                                raise TypeError
                        except (TypeError, json.JSONDecodeError):
                            if in_cohort:
                                malformed_event_keys.add(
                                    (
                                        str(outcome["run_id"]),
                                        int(outcome["sequence"]),
                                    )
                                )
                            continue
                        stage = payload.get("failure_stage")
                        attempt["terminal"] = True
                        if stage in {"INTERPRETATION", "DETERMINISTIC_VALIDATION"}:
                            attempt["failure"] = True
                            error_code = str(payload.get("error_code", ""))
                            attempt["timeout"] = (
                                stage == "INTERPRETATION"
                                and "timeout" in error_code.casefold()
                            )
                        break
                    elif outcome["event_type"] in {"PLAN_VALIDATED", "RESPONSE_READY"}:
                        attempt["terminal"] = True
                        break
                attempts.append(attempt)

        cohort_attempts = [attempt for attempt in attempts if attempt["in_cohort"]]
        failed = [attempt for attempt in cohort_attempts if attempt["failure"]]
        attempts_by_run: defaultdict[str, list[dict[str, Any]]] = defaultdict(list)
        for attempt in attempts:
            attempts_by_run[attempt["run_id"]].append(attempt)
        recovered = []
        for failed_attempt in failed:
            later = [
                item
                for item in attempts_by_run[failed_attempt["run_id"]]
                if item["sequence"] > failed_attempt["sequence"]
            ]
            if any(item["terminal"] and not item["failure"] for item in later):
                recovered.append(failed_attempt)
        final_failed_runs: set[str] = set()
        for run_id in {attempt["run_id"] for attempt in cohort_attempts}:
            terminal_attempts = [
                item for item in attempts_by_run[run_id] if item["terminal"]
            ]
            if not terminal_attempts:
                continue
            latest = max(terminal_attempts, key=lambda item: item["sequence"])
            if latest["final_status"] == "FAILED" and latest["failure"]:
                final_failed_runs.add(run_id)
        return {
            "started_attempts": len(cohort_attempts),
            "completed_attempts": sum(
                1 for attempt in cohort_attempts if attempt["terminal"]
            ),
            "incomplete_attempts": sum(
                1 for attempt in cohort_attempts if not attempt["terminal"]
            ),
            "failures": len(failed),
            "final_failed_requests": len(final_failed_runs),
            "recovered_attempts": len(recovered),
            "timeouts": sum(1 for attempt in failed if attempt["timeout"]),
            "ollama_latencies": [
                int(attempt["latency"])
                for attempt in cohort_attempts
                if attempt["provider"] == "local"
                and isinstance(attempt["latency"], int)
            ],
            "missing_local_durations": sum(
                1
                for attempt in cohort_attempts
                if attempt["provider"] == "local"
                and attempt["interpretation_completed"]
                and attempt["latency"] is None
            ),
            "malformed_event_payloads": len(malformed_event_keys),
        }

    @classmethod
    def _report_metrics(
        cls,
        connection,
        *,
        user_id: str,
        start_iso: str,
        end_iso: str,
        as_of_iso: str,
        findings: list[dict[str, str]],
    ) -> dict[str, Any]:
        all_rows = connection.execute(
            """
            SELECT id, freshness, generation_mode, generation_diagnostic,
                   narration_duration_ms, diagnostics_json, created_at
            FROM report_snapshots
            WHERE user_id = ? AND created_at < ?
            ORDER BY created_at
            """,
            (user_id, as_of_iso),
        ).fetchall()
        rows = [
            row
            for row in all_rows
            if start_iso <= str(row["created_at"]) < end_iso
        ]
        report_by_hash = {
            sha256_text(str(row["id"])): str(row["id"]) for row in all_rows
        }
        cohort_ids = {str(row["id"]) for row in rows}
        corrected_cohort: set[str] = set()
        unmatched_corrections = 0
        for finding in findings:
            if (
                finding["category"] != "REPORT_CORRECTION_REQUIRED"
                or finding["source_type"] == "CONTEXT_CORRECTION_EVENT"
            ):
                continue
            matched_id = report_by_hash.get(finding["source_ref_hash"])
            if matched_id in cohort_ids:
                corrected_cohort.add(matched_id)
            if (
                matched_id is None
                and start_iso <= finding["recorded_at"] < end_iso
            ):
                unmatched_corrections += 1
        structural_anomalies = 0
        malformed_diagnostics = 0
        narration_latencies: list[int] = []
        for row in rows:
            try:
                diagnostic = json.loads(row["diagnostics_json"] or "{}")
                if not isinstance(diagnostic, dict):
                    raise TypeError
                invariant_values = [
                    int(diagnostic.get(field, 0) or 0)
                    for field in cls._REPORT_INVARIANT_FIELDS
                ]
            except (TypeError, ValueError, json.JSONDecodeError):
                structural_anomalies += 1
                malformed_diagnostics += 1
            else:
                if any(value != 0 for value in invariant_values):
                    structural_anomalies += 1
            duration = row["narration_duration_ms"]
            if isinstance(duration, int) and duration >= 0:
                narration_latencies.append(duration)
        return {
            "generated": len(rows),
            "corrected_cohort": len(corrected_cohort),
            "unmatched_corrections": unmatched_corrections,
            "narrator_attempts": sum(
                1
                for row in rows
                if row["narration_duration_ms"] is not None
                and row["generation_mode"] in {"LLM", "TEMPLATE_FALLBACK"}
            ),
            "narrator_timeouts": sum(
                1 for row in rows if row["generation_diagnostic"] == "NARRATOR_TIMEOUT"
            ),
            "structural_anomalies": structural_anomalies,
            "malformed_diagnostics": malformed_diagnostics,
            "stale": sum(1 for row in rows if row["freshness"] == "STALE"),
            "template_fallbacks": sum(
                1 for row in rows if row["generation_mode"] == "TEMPLATE_FALLBACK"
            ),
            "narration_latencies": narration_latencies,
        }

    @staticmethod
    def _recommendation_metrics(
        connection,
        *,
        user_id: str,
        start_iso: str,
        end_iso: str,
    ) -> dict[str, int]:
        rows = connection.execute(
            """
            SELECT result_json
            FROM orchestration_runs
            WHERE user_id = ?
              AND completed_at >= ?
              AND completed_at < ?
              AND result_json IS NOT NULL
            """,
            (user_id, start_iso, end_iso),
        ).fetchall()
        attempts = 0
        timeouts = 0
        malformed_results = 0
        for row in rows:
            try:
                result = json.loads(row["result_json"])
            except (TypeError, json.JSONDecodeError):
                malformed_results += 1
                continue
            if not isinstance(result, dict):
                malformed_results += 1
                continue
            data = result.get("data")
            if data is None:
                continue
            if not isinstance(data, dict):
                malformed_results += 1
                continue
            presentation = data.get("presentation")
            if presentation is None:
                continue
            if not isinstance(presentation, dict):
                malformed_results += 1
                continue
            if presentation.get("mode") not in {
                "NARRATED",
                "DETERMINISTIC_FALLBACK",
            }:
                continue
            attempts += 1
            if presentation.get("fallback_reason") == "NARRATOR_TIMEOUT":
                timeouts += 1
        return {
            "attempts": attempts,
            "timeouts": timeouts,
            "malformed_results": malformed_results,
        }

    @staticmethod
    def _latency(values: list[int]) -> LatencyMetric:
        if not values:
            return LatencyMetric(samples=0, average=None, p95=None)
        ordered = sorted(values)
        p95_index = max(0, ceil(len(ordered) * 0.95) - 1)
        return LatencyMetric(
            samples=len(ordered),
            average=round(sum(ordered) / len(ordered), 1),
            p95=ordered[p95_index],
        )

    @staticmethod
    def _completeness(
        *,
        malformed_decision_count: int,
        malformed_event_payload_count: int,
        incomplete_extraction_attempt_count: int,
        missing_local_duration_count: int,
        unmatched_context_correction_count: int,
        coarse_context_correction_match_count: int,
        unmatched_report_correction_count: int,
        malformed_report_diagnostics_count: int,
        malformed_result_payload_count: int,
    ) -> ValidationCompleteness:
        counts = (
            malformed_decision_count,
            malformed_event_payload_count,
            incomplete_extraction_attempt_count,
            missing_local_duration_count,
            unmatched_context_correction_count,
            coarse_context_correction_match_count,
            unmatched_report_correction_count,
            malformed_report_diagnostics_count,
            malformed_result_payload_count,
        )
        return ValidationCompleteness(
            status="PASS" if all(count == 0 for count in counts) else "PARTIAL",
            malformed_decision_count=malformed_decision_count,
            malformed_event_payload_count=malformed_event_payload_count,
            incomplete_extraction_attempt_count=(
                incomplete_extraction_attempt_count
            ),
            missing_local_duration_count=missing_local_duration_count,
            unmatched_context_correction_count=(
                unmatched_context_correction_count
            ),
            coarse_context_correction_match_count=(
                coarse_context_correction_match_count
            ),
            unmatched_report_correction_count=(
                unmatched_report_correction_count
            ),
            malformed_report_diagnostics_count=(
                malformed_report_diagnostics_count
            ),
            malformed_result_payload_count=malformed_result_payload_count,
        )

    @staticmethod
    def _rate(numerator: int, denominator: int) -> RateMetric:
        return RateMetric(
            numerator=numerator,
            denominator=denominator,
            value=(round(numerator / denominator, 4) if denominator else None),
        )
