from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass

from app.context_linking import LinkDecision
from app.database import Database
from app.models import (
    ClarificationView,
    ExtractionEnvelope,
    JarvisResponse,
    LinkDecisionType,
    ValidatedFactGroup,
)
from app.stabilization import (
    CORRECTION_EVENT_TYPE,
    CORRECTION_SCHEMA_VERSION,
    ContextLinkCorrectionSignal,
    make_regression_case,
    project_mentions_match,
)
from app.utils import canonical_json, new_id, sha256_text, utc_iso


class DuplicateMessageConflict(RuntimeError):
    pass


class ResourceNotFound(RuntimeError):
    pass


class VersionConflict(RuntimeError):
    pass


class RunInProgress(RuntimeError):
    def __init__(self, run_id: str) -> None:
        super().__init__("the same request is already being processed")
        self.run_id = run_id


@dataclass
class RunIntake:
    run_id: str
    message_id: str
    conversation_id: str
    existing_response: JarvisResponse | None
    stored_plan: ExtractionEnvelope | None
    stored_provider_name: str | None
    stored_model_version: str | None
    stored_prompt_version: str | None
    retry_skill: bool = False


@dataclass
class PlannedFactGroup:
    fact_group_id: str
    state_version: int
    draft: ValidatedFactGroup
    decision: LinkDecision
    clarification: ClarificationView | None


class WorkRepository:
    def __init__(self, database: Database) -> None:
        self.database = database

    def create_or_get_run(
        self,
        *,
        user_id: str,
        conversation_id: str | None,
        client_message_id: str,
        content: str,
    ) -> RunIntake:
        content_hash = sha256_text(content)
        now = utc_iso(self.database.clock.now_utc())
        with self.database.transaction() as connection:
            resolved_conversation_id = self._resolve_conversation(
                connection,
                user_id=user_id,
                conversation_id=conversation_id,
                now=now,
            )
            existing_message = connection.execute(
                """
                SELECT id, content_hash
                FROM chat_messages
                WHERE conversation_id = ? AND client_message_id = ?
                """,
                (resolved_conversation_id, client_message_id),
            ).fetchone()
            if existing_message:
                if existing_message["content_hash"] != content_hash:
                    raise DuplicateMessageConflict(
                        "client_message_id was already used with different content"
                    )
                run = connection.execute(
                    """
                    SELECT id,
                           status,
                           result_json,
                           structured_plan_json,
                           provider_name,
                           model_version,
                           prompt_version
                    FROM orchestration_runs
                    WHERE request_message_id = ?
                    """,
                    (existing_message["id"],),
                ).fetchone()
                existing_response = (
                    JarvisResponse.model_validate_json(run["result_json"])
                    if run and run["result_json"]
                    else None
                )
                if existing_response is None and run["status"] not in {
                    "FAILED",
                    "INTERRUPTED_RETRYABLE",
                }:
                    raise RunInProgress(run["id"])
                if existing_response is None:
                    # Claim a retry while the write transaction is held. A
                    # concurrent replay will now observe RECEIVED and cannot
                    # invoke the Provider or overwrite the stored plan.
                    claimed = connection.execute(
                        """
                        UPDATE orchestration_runs
                        SET status = 'RECEIVED',
                            memory_status = NULL,
                            error_code = NULL,
                            completed_at = NULL
                        WHERE id = ?
                          AND user_id = ?
                          AND status IN ('FAILED', 'INTERRUPTED_RETRYABLE')
                        """,
                        (run["id"], user_id),
                    )
                    if claimed.rowcount != 1:
                        raise RunInProgress(run["id"])
                stored_plan = (
                    ExtractionEnvelope.model_validate_json(
                        run["structured_plan_json"]
                    )
                    if run and run["structured_plan_json"]
                    else None
                )
                return RunIntake(
                    run_id=run["id"],
                    message_id=existing_message["id"],
                    conversation_id=resolved_conversation_id,
                    existing_response=existing_response,
                    stored_plan=stored_plan,
                    stored_provider_name=(run["provider_name"] if run else None),
                    stored_model_version=(run["model_version"] if run else None),
                    stored_prompt_version=(run["prompt_version"] if run else None),
                    retry_skill=bool(
                        existing_response is None
                        and run
                        and run["status"] in {"FAILED", "INTERRUPTED_RETRYABLE"}
                        and run["structured_plan_json"] is None
                    ),
                )

            next_sequence = connection.execute(
                """
                SELECT COALESCE(MAX(server_sequence), 0) + 1 AS next_sequence
                FROM chat_messages
                WHERE conversation_id = ?
                """,
                (resolved_conversation_id,),
            ).fetchone()["next_sequence"]
            message_id = new_id("msg")
            run_id = new_id("run")
            connection.execute(
                """
                INSERT INTO chat_messages(
                    id,
                    user_id,
                    conversation_id,
                    server_sequence,
                    role,
                    content,
                    content_hash,
                    client_message_id,
                    created_at
                )
                VALUES (?, ?, ?, ?, 'USER', ?, ?, ?, ?)
                """,
                (
                    message_id,
                    user_id,
                    resolved_conversation_id,
                    next_sequence,
                    content,
                    content_hash,
                    client_message_id,
                    now,
                ),
            )
            connection.execute(
                """
                INSERT INTO orchestration_runs(
                    id,
                    user_id,
                    conversation_id,
                    request_message_id,
                    status,
                    started_at
                )
                VALUES (?, ?, ?, ?, 'RECEIVED', ?)
                """,
                (
                    run_id,
                    user_id,
                    resolved_conversation_id,
                    message_id,
                    now,
                ),
            )
            self._append_event(
                connection,
                user_id=user_id,
                run_id=run_id,
                event_type="REQUEST_RECEIVED",
                public_summary="요청을 접수했습니다.",
                payload={"message_id": message_id},
                now=now,
            )
            return RunIntake(
                run_id=run_id,
                message_id=message_id,
                conversation_id=resolved_conversation_id,
                existing_response=None,
                stored_plan=None,
                stored_provider_name=None,
                stored_model_version=None,
                stored_prompt_version=None,
            )

    def begin_interpretation(
        self,
        *,
        user_id: str,
        run_id: str,
        provider_name: str,
        model_version: str | None,
        prompt_version: str,
    ) -> None:
        now = utc_iso(self.database.clock.now_utc())
        with self.database.transaction() as connection:
            connection.execute(
                """
                UPDATE orchestration_runs
                SET status = 'INTERPRETING',
                    memory_status = NULL,
                    error_code = NULL,
                    completed_at = NULL,
                    provider_name = ?,
                    model_version = ?,
                    prompt_version = ?
                WHERE id = ? AND user_id = ?
                """,
                (
                    provider_name,
                    model_version,
                    prompt_version,
                    run_id,
                    user_id,
                ),
            )
            self._append_event(
                connection,
                user_id=user_id,
                run_id=run_id,
                event_type="INTERPRETATION_STARTED",
                public_summary="업무 요청을 해석하고 있습니다.",
                payload={
                    "provider": provider_name,
                    "model": model_version,
                    "prompt_version": prompt_version,
                },
                now=now,
            )

    def complete_interpretation(
        self,
        *,
        user_id: str,
        run_id: str,
        duration_ms: int,
    ) -> None:
        """Persist a safe provider latency marker without model input/output."""

        now = utc_iso(self.database.clock.now_utc())
        with self.database.transaction() as connection:
            self._append_event(
                connection,
                user_id=user_id,
                run_id=run_id,
                event_type="INTERPRETATION_COMPLETED",
                public_summary="업무 요청 해석을 완료했습니다.",
                payload={"duration_ms": max(0, duration_ms)},
                now=now,
            )

    def begin_skill_execution(
        self,
        *,
        user_id: str,
        run_id: str,
        step_key: str,
        skill_name: str,
        skill_version: str,
        model_profile: str,
        max_iterations: int,
        input_digest: str,
        context_digest: str,
        allow_retry: bool = False,
    ) -> dict:
        """Create or resume the single persisted Skill step for a Run."""

        now = utc_iso(self.database.clock.now_utc())
        with self.database.transaction() as connection:
            existing = connection.execute(
                """
                SELECT *
                FROM skill_executions
                WHERE run_id = ? AND step_key = ? AND user_id = ?
                """,
                (run_id, step_key, user_id),
            ).fetchone()
            if existing is not None:
                if existing["state"] == "RUNNING":
                    raise RunInProgress(run_id)
                if allow_retry and existing["state"] == "COMPLETED":
                    if (
                        existing["skill_name"] != skill_name
                        or existing["skill_version"] != skill_version
                    ):
                        raise VersionConflict("stored Skill definition differs")
                    connection.execute(
                        """
                        UPDATE skill_executions
                        SET state = 'PENDING', iteration = 0, error_code = NULL,
                            output_json = NULL, output_digest = NULL,
                            model_profile = ?, max_iterations = ?,
                            input_digest = ?, context_digest = ?,
                            updated_at = ?, completed_at = NULL
                        WHERE id = ? AND user_id = ?
                        """,
                        (
                            model_profile,
                            max_iterations,
                            input_digest,
                            context_digest,
                            now,
                            existing["id"],
                            user_id,
                        ),
                    )
                    existing = connection.execute(
                        "SELECT * FROM skill_executions WHERE id = ?",
                        (existing["id"],),
                    ).fetchone()
                if existing["state"] in {"FAILED", "INTERRUPTED"}:
                    if (
                        existing["skill_name"] != skill_name
                        or existing["skill_version"] != skill_version
                    ):
                        raise VersionConflict("stored Skill definition differs")
                    connection.execute(
                        """
                        UPDATE skill_executions
                        SET state = 'PENDING', iteration = 0, error_code = NULL,
                            output_json = NULL, output_digest = NULL,
                            model_profile = ?, max_iterations = ?,
                            input_digest = ?, context_digest = ?,
                            updated_at = ?, completed_at = NULL
                        WHERE id = ? AND user_id = ?
                        """,
                        (
                            model_profile,
                            max_iterations,
                            input_digest,
                            context_digest,
                            now,
                            existing["id"],
                            user_id,
                        ),
                    )
                    existing = connection.execute(
                        "SELECT * FROM skill_executions WHERE id = ?",
                        (existing["id"],),
                    ).fetchone()
                else:
                    if (
                        existing["skill_name"] != skill_name
                        or existing["skill_version"] != skill_version
                        or existing["input_digest"] != input_digest
                        or existing["context_digest"] != context_digest
                    ):
                        raise VersionConflict("stored Skill execution input differs")
                return dict(existing)

            execution_id = new_id("skexec")
            connection.execute(
                """
                INSERT INTO skill_executions(
                    id, user_id, run_id, step_key, skill_name, skill_version,
                    model_profile, state, iteration, max_iterations,
                    depends_on_json, input_digest, context_digest,
                    created_at, updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, 'PENDING', 0, ?, '[]', ?, ?, ?, ?)
                """,
                (
                    execution_id,
                    user_id,
                    run_id,
                    step_key,
                    skill_name,
                    skill_version,
                    model_profile,
                    max_iterations,
                    input_digest,
                    context_digest,
                    now,
                    now,
                ),
            )
            self._append_event(
                connection,
                user_id=user_id,
                run_id=run_id,
                event_type="SKILL_SELECTED",
                public_summary=f"{skill_name} Skill을 선택했습니다.",
                payload={
                    "skill": skill_name,
                    "version": skill_version,
                    "model_profile": model_profile,
                    "step_key": step_key,
                },
                now=now,
            )
            return {
                "id": execution_id,
                "output_json": None,
                "state": "PENDING",
            }

    def update_skill_execution(
        self,
        *,
        user_id: str,
        execution_id: str,
        state: str,
        iteration: int,
    ) -> None:
        now = utc_iso(self.database.clock.now_utc())
        with self.database.transaction() as connection:
            connection.execute(
                """
                UPDATE skill_executions
                SET state = ?, iteration = ?, updated_at = ?
                WHERE id = ? AND user_id = ?
                """,
                (state, iteration, now, execution_id, user_id),
            )

    def complete_skill_execution(
        self,
        *,
        user_id: str,
        execution_id: str,
        output_json: str,
        output_digest: str,
        duration_ms: int,
    ) -> None:
        now = utc_iso(self.database.clock.now_utc())
        with self.database.transaction() as connection:
            connection.execute(
                """
                UPDATE skill_executions
                SET state = 'COMPLETED', output_json = ?, output_digest = ?,
                    updated_at = ?, completed_at = ?
                WHERE id = ? AND user_id = ?
                """,
                (output_json, output_digest, now, now, execution_id, user_id),
            )

    def fail_skill_execution(
        self,
        *,
        user_id: str,
        execution_id: str,
        error_code: str,
        duration_ms: int,
    ) -> None:
        del duration_ms
        now = utc_iso(self.database.clock.now_utc())
        with self.database.transaction() as connection:
            connection.execute(
                """
                UPDATE skill_executions
                SET state = 'FAILED', error_code = ?, updated_at = ?, completed_at = ?
                WHERE id = ? AND user_id = ?
                """,
                (error_code[:100], now, now, execution_id, user_id),
            )

    def append_skill_event(
        self,
        *,
        user_id: str,
        run_id: str,
        event_type: str,
        public_summary: str,
        payload: dict | None = None,
    ) -> None:
        now = utc_iso(self.database.clock.now_utc())
        with self.database.transaction() as connection:
            self._append_event(
                connection,
                user_id=user_id,
                run_id=run_id,
                event_type=event_type,
                public_summary=public_summary,
                payload=payload or {},
                now=now,
            )

    def persist_plan(
        self,
        *,
        user_id: str,
        intake: RunIntake,
        envelope: ExtractionEnvelope,
        validated_groups: list[ValidatedFactGroup],
        decisions: list[LinkDecision],
        extractor_version: str,
        provider_name: str = "unknown",
        model_version: str | None = None,
        prompt_version: str = "unknown",
    ) -> list[PlannedFactGroup]:
        if len(validated_groups) != len(decisions):
            raise ValueError("validated groups and decisions must have equal length")
        now = utc_iso(self.database.clock.now_utc())
        plan_json = canonical_json(envelope.model_dump(mode="json"))
        plan_hash = sha256_text(plan_json)
        planned: list[PlannedFactGroup] = []

        with self.database.transaction() as connection:
            existing_run = connection.execute(
                """
                SELECT plan_hash, structured_plan_json
                FROM orchestration_runs
                WHERE id = ? AND user_id = ?
                """,
                (intake.run_id, user_id),
            ).fetchone()
            if existing_run is None:
                raise ResourceNotFound("run not found")
            if existing_run["structured_plan_json"] is not None:
                if existing_run["plan_hash"] != plan_hash:
                    raise VersionConflict(
                        "stored run plan differs from the replayed extraction"
                    )

            existing_rows = connection.execute(
                """
                SELECT *
                FROM work_fact_groups
                WHERE run_id = ? AND user_id = ?
                ORDER BY group_sequence
                """,
                (intake.run_id, user_id),
            ).fetchall()
            if existing_rows:
                for row in existing_rows:
                    decision = LinkDecision.from_json(row["decision_json"])
                    clarification = None
                    clarification_row = connection.execute(
                        """
                        SELECT id, question, candidates_json
                        FROM clarifications
                        WHERE fact_group_id = ?
                          AND user_id = ?
                          AND status = 'OPEN'
                        ORDER BY created_at DESC
                        LIMIT 1
                        """,
                        (row["id"], user_id),
                    ).fetchone()
                    if clarification_row:
                        clarification = ClarificationView(
                            clarification_id=clarification_row["id"],
                            question=clarification_row["question"],
                            candidates=json.loads(
                                clarification_row["candidates_json"]
                            ),
                        )
                    planned.append(
                        PlannedFactGroup(
                            fact_group_id=row["id"],
                            state_version=row["state_version"],
                            draft=ValidatedFactGroup.model_validate_json(
                                row["draft_json"]
                            ),
                            decision=decision,
                            clarification=clarification,
                        )
                    )
                return planned

            # Query, general, and report plans intentionally have no Fact
            # Groups. On recovery the immutable stored plan is already the
            # source of truth, so do not append another event or overwrite it.
            if existing_run["structured_plan_json"] is not None:
                return planned

            connection.execute(
                """
                UPDATE orchestration_runs
                SET status = 'PLANNED',
                    intent = ?,
                    structured_plan_json = ?,
                    plan_hash = ?,
                    extractor_version = ?,
                    provider_name = ?,
                    model_version = ?,
                    prompt_version = ?,
                    schema_version = ?,
                    safe_trace_json = ?
                WHERE id = ? AND user_id = ?
                """,
                (
                    envelope.intent.value,
                    plan_json,
                    plan_hash,
                    extractor_version,
                    provider_name,
                    model_version,
                    prompt_version,
                    envelope.schema_version,
                    canonical_json({"steps": ["INTERPRET", "VALIDATE", "LINK"]}),
                    intake.run_id,
                    user_id,
                ),
            )
            self._append_event(
                connection,
                user_id=user_id,
                run_id=intake.run_id,
                event_type="PLAN_VALIDATED",
                public_summary="업무 후보를 구조화하고 검증했습니다.",
                payload={
                    "schema_version": envelope.schema_version,
                    "fact_group_count": len(validated_groups),
                },
                now=now,
            )

            for index, (group, decision) in enumerate(
                zip(validated_groups, decisions, strict=True),
                start=1,
            ):
                fact_group_id = new_id("fg")
                pending = decision.decision in {
                    LinkDecisionType.NEEDS_CLARIFICATION,
                    LinkDecisionType.UNRESOLVED,
                }
                status = "PENDING_CONFIRMATION" if pending else "READY"
                connection.execute(
                    """
                    INSERT INTO work_fact_groups(
                        id,
                        user_id,
                        run_id,
                        group_sequence,
                        source_message_id,
                        plan_hash,
                        draft_json,
                        decision_json,
                        target_project_id,
                        target_work_item_id,
                        entity_version,
                        focus_version,
                        state_version,
                        status,
                        created_at,
                        updated_at
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, 1, ?, ?, ?)
                    """,
                    (
                        fact_group_id,
                        user_id,
                        intake.run_id,
                        index,
                        intake.message_id,
                        plan_hash,
                        canonical_json(group.model_dump(mode="json")),
                        decision.as_json(),
                        (
                            decision.selected.project_id
                            if decision.selected
                            else None
                        ),
                        (
                            decision.selected.work_item_id
                            if decision.selected
                            else None
                        ),
                        (
                            decision.selected.version
                            if decision.selected
                            else None
                        ),
                        status,
                        now,
                        now,
                    ),
                )
                clarification = None
                if pending:
                    clarification = self._insert_clarification(
                        connection,
                        user_id=user_id,
                        fact_group_id=fact_group_id,
                        plan_hash=plan_hash,
                        group=group,
                        decision=decision,
                        now=now,
                    )
                planned.append(
                    PlannedFactGroup(
                        fact_group_id=fact_group_id,
                        state_version=1,
                        draft=group,
                        decision=decision,
                        clarification=clarification,
                    )
                )

            if any(group.clarification for group in planned):
                connection.execute(
                    """
                    UPDATE orchestration_runs
                    SET status = 'NEEDS_CLARIFICATION',
                        memory_status = 'PENDING_CONFIRMATION'
                    WHERE id = ? AND user_id = ?
                    """,
                    (intake.run_id, user_id),
                )
        return planned

    def complete_run(
        self,
        *,
        user_id: str,
        run_id: str,
        response: JarvisResponse,
        memory_status: str,
    ) -> None:
        now = utc_iso(self.database.clock.now_utc())
        with self.database.transaction() as connection:
            status = (
                "NEEDS_CLARIFICATION"
                if response.status == "NEEDS_CLARIFICATION"
                else "COMPLETED"
            )
            connection.execute(
                """
                UPDATE orchestration_runs
                SET status = ?,
                    memory_status = ?,
                    result_type = 'CHAT',
                    result_json = ?,
                    error_code = NULL,
                    completed_at = ?
                WHERE id = ? AND user_id = ?
                """,
                (
                    status,
                    memory_status,
                    response.model_dump_json(),
                    now,
                    run_id,
                    user_id,
                ),
            )
            self._append_event(
                connection,
                user_id=user_id,
                run_id=run_id,
                event_type="RESPONSE_READY",
                public_summary=response.display_response,
                payload={"status": response.status},
                now=now,
            )

    def attach_run_audio(
        self,
        *,
        user_id: str,
        run_id: str,
        expected_response: JarvisResponse,
        audio_url: str,
        duration_seconds: float | None,
    ) -> None:
        """Attach presentation metadata to an already-terminal chat result.

        The CAS keeps this best-effort presentation update from reopening or
        otherwise changing the run lifecycle and Canonical Memory status.
        """
        expected_json = expected_response.model_dump_json()
        response = expected_response.model_copy(
            update={
                "audio_url": audio_url,
                "audio_duration_seconds": duration_seconds,
            }
        )
        with self.database.transaction() as connection:
            cursor = connection.execute(
                """
                UPDATE orchestration_runs
                SET result_json = ?
                WHERE id = ?
                  AND user_id = ?
                  AND status IN ('COMPLETED', 'NEEDS_CLARIFICATION')
                  AND result_json = ?
                """,
                (response.model_dump_json(), run_id, user_id, expected_json),
            )
            if cursor.rowcount != 1:
                raise VersionConflict("run response changed during audio synthesis")

    def record_context_link_correction(
        self,
        *,
        user_id: str,
        intake: RunIntake,
        signal: ContextLinkCorrectionSignal,
    ) -> dict:
        """Persist an observed project-link correction without mutating Memory.

        The correction is tied to its own orchestration run and points back to
        the most recent applied fact group. This gives operators enough stable
        evidence to reproduce the error while leaving the versioned relink
        flow as the only way to alter Canonical Memory.
        """

        now = utc_iso(self.database.clock.now_utc())
        with self.database.transaction() as connection:
            existing = connection.execute(
                """
                SELECT payload_json
                FROM execution_events
                WHERE user_id = ? AND run_id = ? AND event_type = ?
                ORDER BY sequence
                LIMIT 1
                """,
                (user_id, intake.run_id, CORRECTION_EVENT_TYPE),
            ).fetchone()
            if existing is not None:
                return json.loads(existing["payload_json"])

            context_rows = connection.execute(
                """
                SELECT
                    previous_run.id AS source_run_id,
                    previous_message.id AS source_message_id,
                    previous_message.content AS source_content,
                    wfg.id AS fact_group_id,
                    wfg.group_sequence,
                    wfg.draft_json,
                    wfg.decision_json,
                    wi.id AS work_item_id,
                    wi.title AS work_item_title,
                    p.id AS project_id,
                    p.name AS project_name
                FROM orchestration_runs correction_run
                JOIN chat_messages correction_message
                  ON correction_message.id = correction_run.request_message_id
                 AND correction_message.user_id = correction_run.user_id
                JOIN orchestration_runs previous_run
                  ON previous_run.user_id = correction_run.user_id
                 AND previous_run.conversation_id = correction_run.conversation_id
                JOIN chat_messages previous_message
                  ON previous_message.id = previous_run.request_message_id
                 AND previous_message.user_id = previous_run.user_id
                JOIN work_fact_groups wfg
                  ON wfg.run_id = previous_run.id
                 AND wfg.user_id = previous_run.user_id
                JOIN work_items wi
                  ON wi.id = wfg.target_work_item_id
                 AND wi.user_id = wfg.user_id
                JOIN projects p
                  ON p.id = wi.project_id
                 AND p.user_id = wi.user_id
                WHERE correction_run.id = ?
                  AND correction_run.user_id = ?
                  AND previous_message.server_sequence
                      < correction_message.server_sequence
                  AND wfg.status = 'APPLIED'
                ORDER BY previous_message.server_sequence DESC,
                         wfg.group_sequence DESC
                """,
                (intake.run_id, user_id),
            ).fetchall()
            project_terms: dict[str, list[str]] = {}
            for row in context_rows:
                if row["project_id"] in project_terms:
                    continue
                aliases = connection.execute(
                    """
                    SELECT alias
                    FROM project_aliases
                    WHERE user_id = ? AND project_id = ?
                    """,
                    (user_id, row["project_id"]),
                ).fetchall()
                project_terms[row["project_id"]] = [
                    row["project_name"],
                    *(alias["alias"] for alias in aliases),
                ]
            matching_project_ids = {
                project_id
                for project_id, terms in project_terms.items()
                if any(
                    project_mentions_match(
                        signal.rejected_project_mention,
                        term,
                    )
                    for term in terms
                )
            }
            context = None
            if len(matching_project_ids) == 1:
                matched_project_id = next(iter(matching_project_ids))
                context = next(
                    row
                    for row in context_rows
                    if row["project_id"] == matched_project_id
                )

            activities: list[dict] = []
            decision_summary: dict = {}
            canonical_patch_review_required = False
            if context is not None:
                activity_rows = connection.execute(
                    """
                    SELECT
                        a.id AS activity_id,
                        a.kind,
                        a.summary,
                        al.id AS link_id,
                        al.link_method,
                        al.link_score,
                        al.decision_evidence_json
                    FROM activities a
                    JOIN activity_links al
                      ON al.activity_id = a.id
                     AND al.user_id = a.user_id
                     AND al.is_active = 1
                    WHERE a.user_id = ?
                      AND a.source_message_id = ?
                      AND al.work_item_id = ?
                      AND a.claim_sequence > ?
                      AND a.claim_sequence < ?
                    ORDER BY a.claim_sequence
                    """,
                    (
                        user_id,
                        context["source_message_id"],
                        context["work_item_id"],
                        context["group_sequence"] * 1000,
                        (context["group_sequence"] + 1) * 1000,
                    ),
                ).fetchall()
                activities = [dict(row) for row in activity_rows]
                try:
                    decision = json.loads(context["decision_json"] or "{}")
                except json.JSONDecodeError:
                    decision = {}
                decision_summary = {
                    key: decision.get(key)
                    for key in ("decision", "score", "margin", "evidence")
                    if decision.get(key) is not None
                }
                try:
                    draft = json.loads(context["draft_json"] or "{}")
                except json.JSONDecodeError:
                    draft = {}
                patch = draft.get("proposed_patch") or {}
                canonical_patch_review_required = any(
                    value not in (None, False, "", [], {})
                    for value in patch.values()
                )

            observed_project_name = (
                context["project_name"] if context is not None else None
            )
            case_id, regression_case = make_regression_case(
                signal=signal,
                original_content=(
                    context["source_content"] if context is not None else None
                ),
                observed_project_name=observed_project_name,
                observed_work_item_title=(
                    context["work_item_title"] if context is not None else None
                ),
                observed_link_decision=decision_summary.get("decision"),
                canonical_patch_review_required=(
                    canonical_patch_review_required
                ),
            )
            payload = {
                "schema_version": CORRECTION_SCHEMA_VERSION,
                "case_id": case_id,
                "status": "OPEN",
                "category": "PROJECT_MISCLASSIFICATION",
                "signal": {
                    "rejected_project_mention": (
                        signal.rejected_project_mention
                    ),
                    "expected_project_mention": (
                        signal.expected_project_mention
                    ),
                    "rejected_matches_observed_project": (
                        project_mentions_match(
                            signal.rejected_project_mention,
                            observed_project_name,
                        )
                        if observed_project_name
                        else False
                    ),
                },
                "observed_link": (
                    {
                        "source_run_id": context["source_run_id"],
                        "fact_group_id": context["fact_group_id"],
                        "project_id": context["project_id"],
                        "project_name": observed_project_name,
                        "work_item_id": context["work_item_id"],
                        "work_item_title": context["work_item_title"],
                        "activity_ids": [
                            item["activity_id"] for item in activities
                        ],
                        "canonical_patch_review_required": (
                            canonical_patch_review_required
                        ),
                        "links": activities,
                        "decision": decision_summary,
                    }
                    if context is not None
                    else None
                ),
                "regression_case": regression_case,
            }
            connection.execute(
                """
                UPDATE orchestration_runs
                SET status = 'PLANNED',
                    intent = 'GENERAL',
                    memory_status = 'CORRECTION_RECORDED',
                    extractor_version = 'context-correction-detector-v1',
                    schema_version = ?,
                    provider_name = 'deterministic-policy',
                    model_version = NULL,
                    prompt_version = 'context-correction-v1',
                    safe_trace_json = ?
                WHERE id = ? AND user_id = ?
                """,
                (
                    CORRECTION_SCHEMA_VERSION,
                    canonical_json(
                        [
                            {
                                "phase": "CORRECTION_DETECTION",
                                "outcome": "RECORDED",
                                "case_id": case_id,
                            }
                        ]
                    ),
                    intake.run_id,
                    user_id,
                ),
            )
            self._append_event(
                connection,
                user_id=user_id,
                run_id=intake.run_id,
                event_type=CORRECTION_EVENT_TYPE,
                public_summary="사용자 정정으로 업무 연결 오류 사례를 기록했습니다.",
                payload=payload,
                now=now,
            )
            return payload

    def fail_run(
        self,
        *,
        user_id: str,
        run_id: str,
        error_code: str,
        failure_stage: str = "UNKNOWN",
        duration_ms: int | None = None,
    ) -> None:
        now = utc_iso(self.database.clock.now_utc())
        with self.database.transaction() as connection:
            connection.execute(
                """
                UPDATE orchestration_runs
                SET status = 'FAILED',
                    memory_status = 'FAILED',
                    error_code = ?,
                    completed_at = ?
                WHERE id = ? AND user_id = ?
                """,
                (error_code, now, run_id, user_id),
            )
            payload: dict[str, str | int] = {
                "error_code": error_code,
                "failure_stage": failure_stage,
            }
            if duration_ms is not None:
                payload["duration_ms"] = max(0, duration_ms)
            self._append_event(
                connection,
                user_id=user_id,
                run_id=run_id,
                event_type="REQUEST_FAILED",
                public_summary="요청을 안전하게 처리하지 못했습니다.",
                payload=payload,
                now=now,
            )

    def get_clarification(self, *, user_id: str, clarification_id: str) -> dict:
        connection = self.database.connect()
        try:
            row = connection.execute(
                """
                SELECT
                    c.*,
                    wfg.draft_json,
                    wfg.run_id,
                    wfg.source_message_id,
                    wfg.status AS fact_group_status,
                    r.conversation_id
                FROM clarifications c
                JOIN work_fact_groups wfg
                  ON wfg.id = c.fact_group_id
                 AND wfg.user_id = c.user_id
                JOIN orchestration_runs r
                  ON r.id = wfg.run_id
                 AND r.user_id = wfg.user_id
                WHERE c.user_id = ? AND c.id = ?
                """,
                (user_id, clarification_id),
            ).fetchone()
            if row is None:
                raise ResourceNotFound("clarification not found")
            return dict(row)
        finally:
            connection.close()

    def get_run_context(self, *, user_id: str, run_id: str) -> dict:
        connection = self.database.connect()
        try:
            row = connection.execute(
                """
                SELECT *
                FROM orchestration_runs
                WHERE user_id = ? AND id = ?
                """,
                (user_id, run_id),
            ).fetchone()
            if row is None:
                raise ResourceNotFound("run not found")
            return dict(row)
        finally:
            connection.close()

    def cancel_clarification(
        self,
        *,
        user_id: str,
        clarification_id: str,
        expected_state_version: int,
    ) -> tuple[str, str]:
        now = utc_iso(self.database.clock.now_utc())
        with self.database.transaction() as connection:
            row = connection.execute(
                """
                SELECT c.status,
                       c.fact_group_id,
                       wfg.run_id,
                       r.conversation_id
                FROM clarifications c
                JOIN work_fact_groups wfg
                  ON wfg.id = c.fact_group_id
                 AND wfg.user_id = c.user_id
                JOIN orchestration_runs r
                  ON r.id = wfg.run_id
                 AND r.user_id = wfg.user_id
                WHERE c.id = ? AND c.user_id = ?
                """,
                (clarification_id, user_id),
            ).fetchone()
            if row is None:
                raise ResourceNotFound("clarification not found")
            if row["status"] == "CANCELLED":
                return row["run_id"], row["conversation_id"]
            cancelled = connection.execute(
                """
                UPDATE clarifications
                SET status = 'CANCELLED',
                    state_version = state_version + 1,
                    resolution_json = ?,
                    resolved_at = ?
                WHERE id = ?
                  AND user_id = ?
                  AND status = 'OPEN'
                  AND state_version = ?
                """,
                (
                    canonical_json({"action": "CANCEL"}),
                    now,
                    clarification_id,
                    user_id,
                    expected_state_version,
                ),
            )
            if cancelled.rowcount != 1:
                raise VersionConflict("clarification is not open")
            connection.execute(
                """
                UPDATE work_fact_groups
                SET status = 'REJECTED',
                    state_version = state_version + 1,
                    updated_at = ?
                WHERE id = ?
                  AND user_id = ?
                  AND status = 'PENDING_CONFIRMATION'
                """,
                (now, row["fact_group_id"], user_id),
            )
            self._append_event(
                connection,
                user_id=user_id,
                run_id=row["run_id"],
                event_type="CLARIFICATION_CANCELLED",
                public_summary="확인 대기 중인 업무 기록을 취소했습니다.",
                payload={"clarification_id": clarification_id},
                now=now,
            )
            return row["run_id"], row["conversation_id"]

    def _resolve_conversation(
        self,
        connection,
        *,
        user_id: str,
        conversation_id: str | None,
        now: str,
    ) -> str:
        if conversation_id:
            row = connection.execute(
                """
                SELECT id
                FROM conversations
                WHERE id = ? AND user_id = ?
                """,
                (conversation_id, user_id),
            ).fetchone()
            if row is None:
                raise ResourceNotFound("conversation not found")
            return row["id"]

        existing = connection.execute(
            """
            SELECT id
            FROM conversations
            WHERE user_id = ? AND is_default = 1
            """,
            (user_id,),
        ).fetchone()
        if existing:
            return existing["id"]

        conversation_id = new_id("conv")
        try:
            connection.execute(
                """
                INSERT INTO conversations(
                    id,
                    user_id,
                    is_default,
                    created_at,
                    updated_at
                )
                VALUES (?, ?, 1, ?, ?)
                """,
                (conversation_id, user_id, now, now),
            )
            return conversation_id
        except sqlite3.IntegrityError:
            concurrent = connection.execute(
                """
                SELECT id
                FROM conversations
                WHERE user_id = ? AND is_default = 1
                """,
                (user_id,),
            ).fetchone()
            if concurrent is None:
                raise
            return concurrent["id"]

    def _insert_clarification(
        self,
        connection,
        *,
        user_id: str,
        fact_group_id: str,
        plan_hash: str,
        group: ValidatedFactGroup,
        decision: LinkDecision,
        now: str,
    ) -> ClarificationView:
        clarification_id = new_id("clar")
        candidates = [candidate.to_view() for candidate in decision.candidates[:3]]
        activity_kinds = {activity.kind.value for activity in group.activities}
        if "RESPONSE_RECEIVED" in activity_kinds:
            if len(candidates) > 1:
                question = "말씀하신 답변이 다음 중 어떤 업무에 대한 것인가요?"
            elif len(candidates) == 1:
                candidate = candidates[0]
                question = (
                    f"말씀하신 답변이 ‘{candidate.project_name} > "
                    f"{candidate.work_item_title}’ 업무에 대한 것인가요?"
                )
            else:
                question = "어떤 프로젝트와 업무에 대한 답변인지 알려주세요."
        elif "REQUEST_SENT" in activity_kinds:
            if len(candidates) > 1:
                question = "이 문의를 다음 중 어떤 업무에 연결할까요?"
            elif len(candidates) == 1:
                candidate = candidates[0]
                question = (
                    f"이 문의를 ‘{candidate.project_name} > "
                    f"{candidate.work_item_title}’ 업무에 연결할까요?"
                )
            else:
                question = "이 문의를 연결할 프로젝트와 업무를 알려주세요."
        elif len(candidates) > 1:
            question = "이 업무 기록은 다음 중 어떤 업무에 대한 것인가요?"
        elif len(candidates) == 1:
            candidate = candidates[0]
            question = (
                f"이 업무 기록이 ‘{candidate.project_name} > "
                f"{candidate.work_item_title}’ 업무에 대한 것인가요?"
            )
        else:
            question = "이 기록을 연결할 프로젝트와 업무를 알려주세요."
        connection.execute(
            """
            INSERT INTO clarifications(
                id,
                user_id,
                fact_group_id,
                question,
                candidates_json,
                plan_hash,
                state_version,
                status,
                created_at
            )
            VALUES (?, ?, ?, ?, ?, ?, 1, 'OPEN', ?)
            """,
            (
                clarification_id,
                user_id,
                fact_group_id,
                question,
                canonical_json(
                    [candidate.model_dump(mode="json") for candidate in candidates]
                ),
                plan_hash,
                now,
            ),
        )
        return ClarificationView(
            clarification_id=clarification_id,
            question=question,
            candidates=candidates,
        )

    @staticmethod
    def _append_event(
        connection,
        *,
        user_id: str,
        run_id: str,
        event_type: str,
        public_summary: str,
        payload: dict,
        now: str,
    ) -> None:
        sequence = connection.execute(
            """
            SELECT COALESCE(MAX(sequence), 0) + 1 AS next_sequence
            FROM execution_events
            WHERE run_id = ?
            """,
            (run_id,),
        ).fetchone()["next_sequence"]
        connection.execute(
            """
            INSERT INTO execution_events(
                id,
                user_id,
                run_id,
                sequence,
                event_type,
                public_summary,
                payload_json,
                created_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                new_id("evt"),
                user_id,
                run_id,
                sequence,
                event_type,
                public_summary,
                canonical_json(payload),
                now,
            ),
        )
