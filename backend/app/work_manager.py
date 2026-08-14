from __future__ import annotations

import json

from app.context_linking import LinkDecision
from app.database import Database
from app.models import (
    ActivityKind,
    LinkDecisionType,
    ReceiptView,
    RelinkActivityResponse,
    ValidatedFactGroup,
    WorkItemPatchDraft,
    WorkStatus,
)
from app.repository import ResourceNotFound, VersionConflict
from app.stabilization import (
    CORRECTION_EVENT_TYPE,
    CORRECTION_PROGRESS_EVENT_TYPE,
    CORRECTION_SCHEMA_VERSION,
    project_mentions_match,
)
from app.utils import canonical_json, new_id, normalize_name, sha256_text, utc_iso


ALLOWED_TRANSITIONS: dict[WorkStatus, set[WorkStatus]] = {
    WorkStatus.TODO: {
        WorkStatus.TODO,
        WorkStatus.IN_PROGRESS,
        WorkStatus.WAITING,
        WorkStatus.BLOCKED,
        WorkStatus.HOLD,
        WorkStatus.DONE,
    },
    WorkStatus.IN_PROGRESS: {
        WorkStatus.IN_PROGRESS,
        WorkStatus.WAITING,
        WorkStatus.BLOCKED,
        WorkStatus.HOLD,
        WorkStatus.DONE,
    },
    WorkStatus.WAITING: {
        WorkStatus.WAITING,
        WorkStatus.IN_PROGRESS,
        WorkStatus.BLOCKED,
        WorkStatus.HOLD,
        WorkStatus.DONE,
    },
    WorkStatus.BLOCKED: {
        WorkStatus.BLOCKED,
        WorkStatus.IN_PROGRESS,
        WorkStatus.WAITING,
        WorkStatus.HOLD,
        WorkStatus.DONE,
    },
    WorkStatus.HOLD: {
        WorkStatus.HOLD,
        WorkStatus.TODO,
        WorkStatus.IN_PROGRESS,
        WorkStatus.DONE,
    },
    WorkStatus.DONE: {WorkStatus.DONE},
}


class WorkManager:
    def __init__(self, database: Database) -> None:
        self.database = database

    def apply_ready_group(
        self,
        *,
        user_id: str,
        fact_group_id: str,
        expected_state_version: int,
        group: ValidatedFactGroup,
        decision: LinkDecision,
    ) -> ReceiptView:
        with self.database.transaction() as connection:
            claimed = connection.execute(
                """
                UPDATE work_fact_groups
                SET status = 'APPLYING',
                    state_version = state_version + 1,
                    updated_at = ?
                WHERE id = ?
                  AND user_id = ?
                  AND status = 'READY'
                  AND state_version = ?
                """,
                (
                    utc_iso(self.database.clock.now_utc()),
                    fact_group_id,
                    user_id,
                    expected_state_version,
                ),
            )
            if claimed.rowcount != 1:
                existing = self._existing_receipt(
                    connection,
                    user_id=user_id,
                    fact_group_id=fact_group_id,
                )
                if existing:
                    return existing
                raise VersionConflict("fact group is not available for apply")

            fact_group_row = connection.execute(
                """
                SELECT *
                FROM work_fact_groups
                WHERE id = ? AND user_id = ?
                """,
                (fact_group_id, user_id),
            ).fetchone()
            if fact_group_row is None:
                raise ResourceNotFound("fact group not found")

            return self._apply_group_in_transaction(
                connection,
                user_id=user_id,
                fact_group_row=fact_group_row,
                group=group,
                decision=decision,
                link_method=(
                    "AUTO"
                    if decision.decision == LinkDecisionType.AUTO_LINK
                    else "EXPLICIT"
                ),
            )

    def apply_confirmed_group(
        self,
        *,
        user_id: str,
        clarification_id: str,
        selected_work_item_id: str | None,
        expected_clarification_version: int,
        idempotency_key: str,
        request_hash: str,
        new_project_name: str | None = None,
        new_work_item_title: str | None = None,
    ) -> tuple[ReceiptView, str, str]:
        route = "/api/v1/clarifications/{clarification_id}/resolve"
        with self.database.transaction() as connection:
            existing_key = connection.execute(
                """
                SELECT request_hash, response_json
                FROM request_idempotency
                WHERE user_id = ?
                  AND method = 'POST'
                  AND route_fingerprint = ?
                  AND idempotency_key = ?
                """,
                (user_id, route, idempotency_key),
            ).fetchone()
            if existing_key:
                if existing_key["request_hash"] != request_hash:
                    raise VersionConflict(
                        "idempotency key was reused with another request"
                    )
                if existing_key["response_json"]:
                    stored = json.loads(existing_key["response_json"])
                    return (
                        ReceiptView.model_validate(stored["receipt"]),
                        stored["run_id"],
                        stored["conversation_id"],
                    )

            row = connection.execute(
                """
                SELECT
                    c.*,
                    wfg.draft_json,
                    wfg.run_id,
                    wfg.source_message_id,
                    wfg.plan_hash AS current_plan_hash,
                    wfg.state_version AS fact_group_state_version,
                    wfg.status AS fact_group_status,
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
            now = utc_iso(self.database.clock.now_utc())
            if not existing_key:
                connection.execute(
                    """
                    INSERT INTO request_idempotency(
                        id,
                        user_id,
                        method,
                        route_fingerprint,
                        idempotency_key,
                        request_hash,
                        status,
                        created_at,
                        updated_at
                    )
                    VALUES (?, ?, 'POST', ?, ?, ?, 'IN_PROGRESS', ?, ?)
                    """,
                    (
                        new_id("idem"),
                        user_id,
                        route,
                        idempotency_key,
                        request_hash,
                        now,
                        now,
                    ),
                )
            if row["plan_hash"] != row["current_plan_hash"]:
                raise VersionConflict("clarification plan became stale")
            if row["status"] == "RESOLVED":
                receipt = self._existing_receipt(
                    connection,
                    user_id=user_id,
                    fact_group_id=row["fact_group_id"],
                )
                if receipt is None:
                    raise VersionConflict("resolved clarification has no receipt")
                self._complete_confirmation_idempotency(
                    connection,
                    user_id=user_id,
                    route=route,
                    idempotency_key=idempotency_key,
                    receipt=receipt,
                    run_id=row["run_id"],
                    conversation_id=row["conversation_id"],
                    now=now,
                )
                return receipt, row["run_id"], row["conversation_id"]
            if (
                row["status"] != "OPEN"
                or row["state_version"] != expected_clarification_version
                or row["fact_group_status"] != "PENDING_CONFIRMATION"
            ):
                raise VersionConflict("clarification is not open")

            creating_new = selected_work_item_id is None
            selected_candidate = None
            current_item = None
            if creating_new:
                if (
                    not new_project_name
                    or not normalize_name(new_project_name)
                    or not new_work_item_title
                    or not normalize_name(new_work_item_title)
                ):
                    raise ValueError(
                        "new work requires an explicit project and work item"
                    )
                existing_named_item = connection.execute(
                    """
                    SELECT wi.id
                    FROM work_items wi
                    JOIN projects p
                      ON p.id = wi.project_id
                     AND p.user_id = wi.user_id
                    LEFT JOIN project_aliases pa
                      ON pa.project_id = p.id
                     AND pa.user_id = p.user_id
                    WHERE wi.user_id = ?
                      AND wi.archived_at IS NULL
                      AND p.archived_at IS NULL
                      AND wi.normalized_title = ?
                      AND (
                        p.normalized_name = ?
                        OR pa.normalized_alias = ?
                      )
                    LIMIT 1
                    """,
                    (
                        user_id,
                        normalize_name(new_work_item_title),
                        normalize_name(new_project_name),
                        normalize_name(new_project_name),
                    ),
                ).fetchone()
                if existing_named_item is not None:
                    raise VersionConflict(
                        "an active work item with the same project and title exists"
                    )
            else:
                candidate_rows = json.loads(row["candidates_json"])
                selected_candidate = next(
                    (
                        candidate
                        for candidate in candidate_rows
                        if candidate["work_item_id"] == selected_work_item_id
                    ),
                    None,
                )
                if selected_candidate is None:
                    raise VersionConflict("selected work item is not a candidate")
                current_item = connection.execute(
                    """
                    SELECT wi.*, p.name AS project_name
                    FROM work_items wi
                    JOIN projects p
                      ON p.id = wi.project_id
                     AND p.user_id = wi.user_id
                    WHERE wi.id = ? AND wi.user_id = ?
                    """,
                    (selected_work_item_id, user_id),
                ).fetchone()
                if current_item is None:
                    raise ResourceNotFound("selected work item not found")
                if current_item["version"] != selected_candidate["version"]:
                    raise VersionConflict("clarification candidate became stale")

            claimed = connection.execute(
                """
                UPDATE clarifications
                SET status = 'RESOLVED',
                    state_version = state_version + 1,
                    resolution_json = ?,
                    resolved_at = ?
                WHERE id = ?
                  AND user_id = ?
                  AND status = 'OPEN'
                  AND state_version = ?
                """,
                (
                    canonical_json(
                        {
                            "action": (
                                "CREATE_NEW" if creating_new else "SELECT_EXISTING"
                            ),
                            "work_item_id": selected_work_item_id,
                            "project_name": (
                                new_project_name if creating_new else None
                            ),
                            "work_item_title": (
                                new_work_item_title if creating_new else None
                            ),
                        }
                    ),
                    utc_iso(self.database.clock.now_utc()),
                    clarification_id,
                    user_id,
                    expected_clarification_version,
                ),
            )
            if claimed.rowcount != 1:
                raise VersionConflict("clarification was resolved concurrently")
            fact_claimed = connection.execute(
                """
                UPDATE work_fact_groups
                SET status = 'APPLYING',
                    state_version = state_version + 1,
                    target_project_id = ?,
                    target_work_item_id = ?,
                    entity_version = ?,
                    updated_at = ?
                WHERE id = ?
                  AND user_id = ?
                  AND status = 'PENDING_CONFIRMATION'
                  AND state_version = ?
                """,
                (
                    current_item["project_id"] if current_item else None,
                    selected_work_item_id,
                    current_item["version"] if current_item else None,
                    utc_iso(self.database.clock.now_utc()),
                    row["fact_group_id"],
                    user_id,
                    row["fact_group_state_version"],
                ),
            )
            if fact_claimed.rowcount != 1:
                raise VersionConflict("fact group was resolved concurrently")

            group = ValidatedFactGroup.model_validate_json(row["draft_json"])
            if creating_new:
                group = group.model_copy(
                    update={
                        "project_mention": new_project_name,
                        "work_item_mention": new_work_item_title,
                    }
                )
            decision = LinkDecision(
                decision=(
                    LinkDecisionType.CREATE_NEW
                    if creating_new
                    else LinkDecisionType.AUTO_LINK
                ),
                selected=None,
                candidates=[],
                score=(
                    None
                    if selected_candidate is None
                    else float(selected_candidate["score"])
                ),
                margin=None,
                evidence=[
                    "USER_CONFIRMED_CREATE_NEW"
                    if creating_new
                    else "USER_CONFIRMED"
                ],
            )
            fact_group_row = connection.execute(
                """
                SELECT *
                FROM work_fact_groups
                WHERE id = ? AND user_id = ?
                """,
                (row["fact_group_id"], user_id),
            ).fetchone()
            receipt = self._apply_group_in_transaction(
                connection,
                user_id=user_id,
                fact_group_row=fact_group_row,
                group=group,
                decision=decision,
                link_method=("EXPLICIT" if creating_new else "USER_CONFIRMED"),
                confirmed_target=(dict(current_item) if current_item else None),
            )
            self._complete_confirmation_idempotency(
                connection,
                user_id=user_id,
                route=route,
                idempotency_key=idempotency_key,
                receipt=receipt,
                run_id=row["run_id"],
                conversation_id=row["conversation_id"],
                now=now,
            )
            return receipt, row["run_id"], row["conversation_id"]

    @staticmethod
    def _complete_confirmation_idempotency(
        connection,
        *,
        user_id: str,
        route: str,
        idempotency_key: str,
        receipt: ReceiptView,
        run_id: str,
        conversation_id: str,
        now: str,
    ) -> None:
        connection.execute(
            """
            UPDATE request_idempotency
            SET status = 'COMPLETED',
                response_reference = ?,
                response_json = ?,
                updated_at = ?
            WHERE user_id = ?
              AND method = 'POST'
              AND route_fingerprint = ?
              AND idempotency_key = ?
            """,
            (
                receipt.receipt_id,
                canonical_json(
                    {
                        "receipt": receipt.model_dump(mode="json"),
                        "run_id": run_id,
                        "conversation_id": conversation_id,
                    }
                ),
                now,
                user_id,
                route,
                idempotency_key,
            ),
        )

    def relink_activity(
        self,
        *,
        user_id: str,
        activity_id: str,
        target_work_item_id: str,
        expected_activity_version: int,
        expected_link_version: int,
        reason: str,
        correction_run_id: str | None = None,
        idempotency_key: str,
        request_hash: str,
    ) -> RelinkActivityResponse:
        now = utc_iso(self.database.clock.now_utc())
        route = "/api/v1/activities/{activity_id}/relink"
        with self.database.transaction() as connection:
            existing_key = connection.execute(
                """
                SELECT request_hash, response_json
                FROM request_idempotency
                WHERE user_id = ?
                  AND method = 'POST'
                  AND route_fingerprint = ?
                  AND idempotency_key = ?
                """,
                (user_id, route, idempotency_key),
            ).fetchone()
            if existing_key:
                if existing_key["request_hash"] != request_hash:
                    raise VersionConflict(
                        "idempotency key was reused with another request"
                    )
                if existing_key["response_json"]:
                    return RelinkActivityResponse.model_validate_json(
                        existing_key["response_json"]
                    )

            activity = connection.execute(
                """
                SELECT *
                FROM activities
                WHERE id = ? AND user_id = ?
                """,
                (activity_id, user_id),
            ).fetchone()
            if activity is None:
                raise ResourceNotFound("activity not found")
            if activity["version"] != expected_activity_version:
                raise VersionConflict("activity version changed")
            old_link = connection.execute(
                """
                SELECT *
                FROM activity_links
                WHERE activity_id = ? AND user_id = ? AND is_active = 1
                """,
                (activity_id, user_id),
            ).fetchone()
            if old_link is None:
                raise ResourceNotFound("active activity link not found")
            if old_link["version"] != expected_link_version:
                raise VersionConflict("activity link version changed")
            target = connection.execute(
                """
                SELECT wi.id, p.id AS project_id, p.name AS project_name
                FROM work_items wi
                JOIN projects p
                  ON p.id = wi.project_id
                 AND p.user_id = wi.user_id
                WHERE wi.id = ?
                  AND wi.user_id = ?
                  AND wi.archived_at IS NULL
                  AND p.archived_at IS NULL
                """,
                (target_work_item_id, user_id),
            ).fetchone()
            if target is None:
                raise ResourceNotFound("target work item not found")

            correction_case_id: str | None = None
            if correction_run_id is not None:
                correction_event = connection.execute(
                    """
                    SELECT payload_json
                    FROM execution_events
                    WHERE user_id = ?
                      AND run_id = ?
                      AND event_type = ?
                    ORDER BY sequence
                    LIMIT 1
                    """,
                    (user_id, correction_run_id, CORRECTION_EVENT_TYPE),
                ).fetchone()
                if correction_event is None:
                    raise ResourceNotFound("context-link correction run not found")
                try:
                    correction_payload = json.loads(
                        correction_event["payload_json"]
                    )
                except json.JSONDecodeError as exc:
                    raise VersionConflict(
                        "context-link correction record is invalid"
                    ) from exc
                observed_link = correction_payload.get("observed_link") or {}
                if activity_id not in observed_link.get("activity_ids", []):
                    raise VersionConflict(
                        "activity is not part of the reported context-link error"
                    )
                expected_project = (
                    correction_payload.get("signal", {}).get(
                        "expected_project_mention"
                    )
                )
                matching_project_ids = (
                    self._matching_active_project_ids(
                        connection,
                        user_id=user_id,
                        project_mention=expected_project,
                    )
                    if isinstance(expected_project, str)
                    else set()
                )
                if len(matching_project_ids) != 1:
                    raise VersionConflict(
                        "reported project correction is not uniquely resolvable"
                    )
                if target["project_id"] not in matching_project_ids:
                    raise VersionConflict(
                        "relink target does not match the reported project correction"
                    )
                correction_case_id = correction_payload.get("case_id")

            if not existing_key:
                connection.execute(
                    """
                    INSERT INTO request_idempotency(
                        id,
                        user_id,
                        method,
                        route_fingerprint,
                        idempotency_key,
                        request_hash,
                        status,
                        created_at,
                        updated_at
                    )
                    VALUES (?, ?, 'POST', ?, ?, ?, 'IN_PROGRESS', ?, ?)
                    """,
                    (
                        new_id("idem"),
                        user_id,
                        route,
                        idempotency_key,
                        request_hash,
                        now,
                        now,
                    ),
                )

            connection.execute(
                """
                UPDATE activity_links
                SET is_active = 0, version = version + 1
                WHERE id = ? AND user_id = ? AND version = ?
                """,
                (old_link["id"], user_id, expected_link_version),
            )
            new_link_id = new_id("alink")
            connection.execute(
                """
                INSERT INTO activity_links(
                    id,
                    user_id,
                    activity_id,
                    work_item_id,
                    link_method,
                    link_score,
                    decision_evidence_json,
                    is_active,
                    version,
                    supersedes_link_id,
                    created_at
                )
                VALUES (?, ?, ?, ?, 'CORRECTION', NULL, ?, 1, 1, ?, ?)
                """,
                (
                    new_link_id,
                    user_id,
                    activity_id,
                    target_work_item_id,
                    canonical_json(
                        {
                            "reason": reason,
                            "correction_run_id": correction_run_id,
                            "correction_case_id": correction_case_id,
                        }
                    ),
                    old_link["id"],
                    now,
                ),
            )
            original_audit = connection.execute(
                """
                SELECT fact_group_id, receipt_id, id
                FROM change_audit
                WHERE user_id = ?
                  AND target_type = 'ACTIVITY'
                  AND target_id = ?
                  AND operation = 'ADD_ACTIVITY'
                ORDER BY created_at
                LIMIT 1
                """,
                (user_id, activity_id),
            ).fetchone()
            if original_audit:
                connection.execute(
                    """
                    INSERT INTO change_audit(
                        id,
                        user_id,
                        fact_group_id,
                        receipt_id,
                        operation,
                        target_type,
                        target_id,
                        expected_version,
                        applied_version,
                        before_json,
                        after_json,
                        correction_of_id,
                        created_at
                    )
                    VALUES (?, ?, ?, ?, 'RELINK_ACTIVITY', 'ACTIVITY_LINK', ?,
                            ?, 1, ?, ?, ?, ?)
                    """,
                    (
                        new_id("audit"),
                        user_id,
                        original_audit["fact_group_id"],
                        original_audit["receipt_id"],
                        new_link_id,
                        expected_link_version,
                        canonical_json(
                            {
                                "link_id": old_link["id"],
                                "work_item_id": old_link["work_item_id"],
                            }
                        ),
                        canonical_json(
                            {
                                "link_id": new_link_id,
                                "work_item_id": target_work_item_id,
                            }
                        ),
                        original_audit["id"],
                        now,
                    ),
                )

            self._recalculate_last_activity(
                connection, user_id, old_link["work_item_id"], now
            )
            self._recalculate_last_activity(
                connection, user_id, target_work_item_id, now
            )
            self._refresh_fts(connection, user_id, old_link["work_item_id"])
            self._refresh_fts(connection, user_id, target_work_item_id)

            response = RelinkActivityResponse(
                activity_id=activity_id,
                previous_work_item_id=old_link["work_item_id"],
                target_work_item_id=target_work_item_id,
                new_link_id=new_link_id,
            )
            if correction_run_id is not None:
                observed_activity_ids = observed_link.get("activity_ids", [])
                observed_work_item_id = observed_link.get("work_item_id")
                remaining_activity_ids: list[str] = []
                if observed_activity_ids and observed_work_item_id:
                    placeholders = ",".join("?" for _ in observed_activity_ids)
                    rows = connection.execute(
                        f"""
                        SELECT activity_id
                        FROM activity_links
                        WHERE user_id = ?
                          AND is_active = 1
                          AND work_item_id = ?
                          AND activity_id IN ({placeholders})
                        ORDER BY activity_id
                        """,
                        (user_id, observed_work_item_id, *observed_activity_ids),
                    ).fetchall()
                    remaining_activity_ids = [
                        row["activity_id"] for row in rows
                    ]
                correction_status = (
                    "ACTIVITY_LINKS_PARTIALLY_RELINKED"
                    if remaining_activity_ids
                    else "ACTIVITY_LINKS_RELINKED"
                )
                canonical_patch_review_required = bool(
                    observed_link.get("canonical_patch_review_required")
                )
                self._append_run_event(
                    connection,
                    user_id=user_id,
                    run_id=correction_run_id,
                    event_type=CORRECTION_PROGRESS_EVENT_TYPE,
                    public_summary=(
                        "Activity 연결 일부를 교정했습니다."
                        if remaining_activity_ids
                        else (
                            "보고된 Activity 연결을 모두 교정했습니다. 기존 상태 변경은 "
                            "별도 검토가 필요합니다."
                            if canonical_patch_review_required
                            else "보고된 Activity 연결을 모두 교정했습니다."
                        )
                    ),
                    payload={
                        "schema_version": CORRECTION_SCHEMA_VERSION,
                        "case_id": correction_case_id,
                        "status": correction_status,
                        "activity_id": activity_id,
                        "previous_work_item_id": old_link["work_item_id"],
                        "target_work_item_id": target_work_item_id,
                        "new_link_id": new_link_id,
                        "remaining_activity_ids": remaining_activity_ids,
                        "remaining_canonical_patch_review": (
                            canonical_patch_review_required
                        ),
                    },
                    now=now,
                )
            connection.execute(
                """
                UPDATE request_idempotency
                SET status = 'COMPLETED',
                    response_reference = ?,
                    response_json = ?,
                    updated_at = ?
                WHERE user_id = ?
                  AND method = 'POST'
                  AND route_fingerprint = ?
                  AND idempotency_key = ?
                """,
                (
                    new_link_id,
                    response.model_dump_json(),
                    now,
                    user_id,
                    route,
                    idempotency_key,
                ),
            )
            return response

    def _apply_group_in_transaction(
        self,
        connection,
        *,
        user_id: str,
        fact_group_row,
        group: ValidatedFactGroup,
        decision: LinkDecision,
        link_method: str,
        confirmed_target: dict | None = None,
    ) -> ReceiptView:
        now = utc_iso(self.database.clock.now_utc())
        created_project = False
        created_work_item = False
        project_audit_id: str | None = None

        if confirmed_target is not None:
            work_item_row = confirmed_target
            project_row = connection.execute(
                """
                SELECT *
                FROM projects
                WHERE id = ? AND user_id = ?
                """,
                (work_item_row["project_id"], user_id),
            ).fetchone()
        elif decision.decision == LinkDecisionType.CREATE_NEW:
            project_row, created_project = self._get_or_create_project(
                connection,
                user_id=user_id,
                project_name=group.project_mention,
                now=now,
            )
            work_item_row, created_work_item = self._get_or_create_work_item(
                connection,
                user_id=user_id,
                project_id=project_row["id"],
                title=group.work_item_mention,
                patch=group.proposed_patch,
                activities=group.activities,
                now=now,
            )
        else:
            target_id = fact_group_row["target_work_item_id"]
            if not target_id and decision.selected:
                target_id = decision.selected.work_item_id
            work_item_row = connection.execute(
                """
                SELECT *
                FROM work_items
                WHERE id = ? AND user_id = ?
                """,
                (target_id, user_id),
            ).fetchone()
            if work_item_row is None:
                raise ResourceNotFound("target work item not found")
            expected_version = fact_group_row["entity_version"]
            if expected_version is not None and (
                work_item_row["version"] != expected_version
            ):
                raise VersionConflict("target work item version changed")
            project_row = connection.execute(
                """
                SELECT *
                FROM projects
                WHERE id = ? AND user_id = ?
                """,
                (work_item_row["project_id"], user_id),
            ).fetchone()

        before = None if created_work_item else dict(work_item_row)
        if not created_work_item:
            work_item_row = self._patch_existing_work_item(
                connection,
                row=work_item_row,
                patch=group.proposed_patch,
                activities=group.activities,
                now=now,
            )

        activity_ids: list[str] = []
        activity_summaries: list[str] = []
        group_sequence = fact_group_row["group_sequence"]
        for index, activity in enumerate(group.activities, start=1):
            activity_id = new_id("act")
            claim_sequence = group_sequence * 1000 + index
            connection.execute(
                """
                INSERT INTO activities(
                    id,
                    user_id,
                    occurred_on_local,
                    occurred_at_utc,
                    timezone,
                    recorded_at_utc,
                    kind,
                    summary,
                    validity,
                    version,
                    source_message_id,
                    claim_sequence,
                    source_excerpt,
                    source_excerpt_hash,
                    derivation,
                    rule_id,
                    created_at
                )
                VALUES (?, ?, ?, NULL, ?, ?, ?, ?, 'ACTIVE', 1, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    activity_id,
                    user_id,
                    activity.occurred_on_local.isoformat(),
                    self.database.timezone_name,
                    now,
                    activity.kind.value,
                    activity.summary,
                    fact_group_row["source_message_id"],
                    claim_sequence,
                    activity.source_excerpt,
                    sha256_text(activity.source_excerpt),
                    activity.derivation.value,
                    activity.rule_id,
                    now,
                ),
            )
            link_id = new_id("alink")
            connection.execute(
                """
                INSERT INTO activity_links(
                    id,
                    user_id,
                    activity_id,
                    work_item_id,
                    link_method,
                    link_score,
                    decision_evidence_json,
                    is_active,
                    version,
                    created_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, 1, 1, ?)
                """,
                (
                    link_id,
                    user_id,
                    activity_id,
                    work_item_row["id"],
                    link_method,
                    decision.score,
                    canonical_json(decision.evidence),
                    now,
                ),
            )
            activity_ids.append(activity_id)
            activity_summaries.append(activity.summary)

        if group.activities:
            latest_activity = max(
                activity.occurred_on_local for activity in group.activities
            ).isoformat()
            if created_work_item:
                connection.execute(
                    """
                    UPDATE work_items
                    SET last_activity_on = ?, updated_at = ?
                    WHERE id = ? AND user_id = ?
                    """,
                    (latest_activity, now, work_item_row["id"], user_id),
                )
            work_item_row = connection.execute(
                """
                SELECT *
                FROM work_items
                WHERE id = ? AND user_id = ?
                """,
                (work_item_row["id"], user_id),
            ).fetchone()

        after = dict(work_item_row)
        changed_fields = (
            [
                "project",
                "title",
                "status",
                "priority",
                "waiting_for",
                "blocked_reason",
                "next_action",
            ]
            if created_work_item
            else self._changed_fields(before, after)
        )
        receipt_id = new_id("receipt")
        receipt = ReceiptView(
            receipt_id=receipt_id,
            fact_group_id=fact_group_row["id"],
            project_id=project_row["id"],
            project_name=project_row["name"],
            work_item_id=work_item_row["id"],
            work_item_title=work_item_row["title"],
            link_decision=(
                LinkDecisionType.AUTO_LINK
                if link_method == "USER_CONFIRMED"
                else decision.decision
            ),
            link_score=decision.score,
            created_project=created_project,
            created_work_item=created_work_item,
            activity_ids=activity_ids,
            activity_summaries=activity_summaries,
            status_before=(WorkStatus(before["status"]) if before else None),
            status_after=WorkStatus(after["status"]),
            waiting_for=after["waiting_for"],
            next_action=after["next_action"],
            changed_fields=changed_fields,
        )
        connection.execute(
            """
            INSERT INTO change_receipts(
                id,
                user_id,
                fact_group_id,
                changed_field_mask_json,
                entity_versions_json,
                created_entity_ids_json,
                result_json,
                created_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                receipt_id,
                user_id,
                fact_group_row["id"],
                canonical_json(changed_fields),
                canonical_json(
                    {
                        "project": project_row["version"],
                        "work_item": after["version"],
                    }
                ),
                canonical_json(
                    {
                        "project_id": project_row["id"] if created_project else None,
                        "work_item_id": (
                            work_item_row["id"] if created_work_item else None
                        ),
                        "activity_ids": activity_ids,
                    }
                ),
                receipt.model_dump_json(),
                now,
            ),
        )

        if created_project:
            project_audit_id = self._insert_audit(
                connection,
                user_id=user_id,
                fact_group_id=fact_group_row["id"],
                receipt_id=receipt_id,
                operation="CREATE_PROJECT",
                target_type="PROJECT",
                target_id=project_row["id"],
                expected_version=None,
                applied_version=project_row["version"],
                before=None,
                after=dict(project_row),
                now=now,
            )
            connection.execute(
                """
                UPDATE project_aliases
                SET source_change_audit_id = ?
                WHERE project_id = ? AND user_id = ?
                """,
                (project_audit_id, project_row["id"], user_id),
            )

        self._insert_audit(
            connection,
            user_id=user_id,
            fact_group_id=fact_group_row["id"],
            receipt_id=receipt_id,
            operation=("CREATE_WORK_ITEM" if created_work_item else "PATCH_WORK_ITEM"),
            target_type="WORK_ITEM",
            target_id=work_item_row["id"],
            expected_version=(before["version"] if before else None),
            applied_version=after["version"],
            before=before,
            after=after,
            now=now,
        )
        for activity_id in activity_ids:
            self._insert_audit(
                connection,
                user_id=user_id,
                fact_group_id=fact_group_row["id"],
                receipt_id=receipt_id,
                operation="ADD_ACTIVITY",
                target_type="ACTIVITY",
                target_id=activity_id,
                expected_version=None,
                applied_version=1,
                before=None,
                after={"activity_id": activity_id, "work_item_id": work_item_row["id"]},
                now=now,
            )

        connection.execute(
            """
            UPDATE work_fact_groups
            SET status = 'APPLIED',
                state_version = state_version + 1,
                target_project_id = ?,
                target_work_item_id = ?,
                entity_version = ?,
                updated_at = ?
            WHERE id = ? AND user_id = ? AND status = 'APPLYING'
            """,
            (
                project_row["id"],
                work_item_row["id"],
                after["version"],
                now,
                fact_group_row["id"],
                user_id,
            ),
        )
        self._refresh_fts(connection, user_id, work_item_row["id"])
        return receipt

    def _get_or_create_project(
        self,
        connection,
        *,
        user_id: str,
        project_name: str | None,
        now: str,
    ):
        if not project_name:
            raise ValueError("new work requires an explicit project")
        normalized = normalize_name(project_name)
        row = connection.execute(
            """
            SELECT DISTINCT p.*
            FROM projects p
            LEFT JOIN project_aliases pa ON pa.project_id = p.id
            WHERE p.user_id = ?
              AND p.archived_at IS NULL
              AND (
                p.normalized_name = ?
                OR pa.normalized_alias = ?
              )
            LIMIT 1
            """,
            (user_id, normalized, normalized),
        ).fetchone()
        if row:
            return row, False

        project_id = new_id("proj")
        connection.execute(
            """
            INSERT INTO projects(
                id,
                user_id,
                name,
                normalized_name,
                version,
                created_at,
                updated_at
            )
            VALUES (?, ?, ?, ?, 1, ?, ?)
            """,
            (project_id, user_id, project_name, normalized, now, now),
        )
        connection.execute(
            """
            INSERT INTO project_aliases(
                id,
                user_id,
                project_id,
                alias,
                normalized_alias,
                created_at
            )
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (new_id("alias"), user_id, project_id, project_name, normalized, now),
        )
        row = connection.execute(
            "SELECT * FROM projects WHERE id = ?",
            (project_id,),
        ).fetchone()
        return row, True

    def _get_or_create_work_item(
        self,
        connection,
        *,
        user_id: str,
        project_id: str,
        title: str | None,
        patch: WorkItemPatchDraft,
        activities,
        now: str,
    ):
        if not title:
            raise ValueError("new work requires an explicit work item")
        normalized = normalize_name(title)
        row = connection.execute(
            """
            SELECT *
            FROM work_items
            WHERE user_id = ?
              AND project_id = ?
              AND normalized_title = ?
              AND archived_at IS NULL
            LIMIT 1
            """,
            (user_id, project_id, normalized),
        ).fetchone()
        if row:
            return row, False

        status = patch.status or (
            WorkStatus.IN_PROGRESS
            if any(
                activity.kind == ActivityKind.WORK_PERFORMED
                for activity in activities
            )
            else WorkStatus.TODO
        )
        waiting_for = patch.waiting_for
        blocked_reason = patch.blocked_reason
        next_action = patch.next_action
        completed_at = now if status == WorkStatus.DONE else None
        self._validate_state(
            status=status,
            waiting_for=waiting_for,
            blocked_reason=blocked_reason,
            next_action=next_action,
            completed_at=completed_at,
        )
        work_item_id = new_id("work")
        connection.execute(
            """
            INSERT INTO work_items(
                id,
                user_id,
                project_id,
                title,
                normalized_title,
                status,
                priority,
                waiting_for,
                blocked_reason,
                next_action,
                version,
                status_changed_at,
                completed_at,
                created_at,
                updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?, ?, ?)
            """,
            (
                work_item_id,
                user_id,
                project_id,
                title,
                normalized,
                status.value,
                (patch.priority.value if patch.priority else "NORMAL"),
                waiting_for,
                blocked_reason,
                next_action,
                now,
                completed_at,
                now,
                now,
            ),
        )
        row = connection.execute(
            "SELECT * FROM work_items WHERE id = ?",
            (work_item_id,),
        ).fetchone()
        return row, True

    def _patch_existing_work_item(
        self,
        connection,
        *,
        row,
        patch: WorkItemPatchDraft,
        activities,
        now: str,
    ):
        current_status = WorkStatus(row["status"])
        new_status = patch.status or current_status
        if new_status not in ALLOWED_TRANSITIONS[current_status]:
            raise VersionConflict(
                f"transition {current_status.value}->{new_status.value} is not allowed"
            )

        waiting_for = row["waiting_for"]
        blocked_reason = row["blocked_reason"]
        next_action = row["next_action"]
        priority = row["priority"]
        completed_at = row["completed_at"]

        if patch.clear_waiting_for:
            waiting_for = None
        if patch.clear_blocked_reason:
            blocked_reason = None
        if patch.waiting_for is not None:
            waiting_for = patch.waiting_for.strip()
        if patch.blocked_reason is not None:
            blocked_reason = patch.blocked_reason.strip()
        if patch.next_action is not None:
            next_action = patch.next_action.strip()
        if patch.priority is not None:
            priority = patch.priority.value

        if new_status == WorkStatus.DONE:
            waiting_for = None
            blocked_reason = None
            next_action = None
            completed_at = now
        elif current_status == WorkStatus.DONE and new_status != WorkStatus.DONE:
            raise VersionConflict("DONE reopen requires an explicit correction flow")
        else:
            completed_at = None

        self._validate_state(
            status=new_status,
            waiting_for=waiting_for,
            blocked_reason=blocked_reason,
            next_action=next_action,
            completed_at=completed_at,
        )
        latest_activity = (
            max(activity.occurred_on_local for activity in activities).isoformat()
            if activities
            else row["last_activity_on"]
        )
        status_changed_at = (
            now if new_status.value != row["status"] else row["status_changed_at"]
        )
        updated = connection.execute(
            """
            UPDATE work_items
            SET status = ?,
                priority = ?,
                waiting_for = ?,
                blocked_reason = ?,
                next_action = ?,
                version = version + 1,
                status_changed_at = ?,
                last_activity_on = ?,
                completed_at = ?,
                updated_at = ?
            WHERE id = ? AND user_id = ? AND version = ?
            """,
            (
                new_status.value,
                priority,
                waiting_for,
                blocked_reason,
                next_action,
                status_changed_at,
                latest_activity,
                completed_at,
                now,
                row["id"],
                row["user_id"],
                row["version"],
            ),
        )
        if updated.rowcount != 1:
            raise VersionConflict("work item changed concurrently")
        return connection.execute(
            """
            SELECT *
            FROM work_items
            WHERE id = ? AND user_id = ?
            """,
            (row["id"], row["user_id"]),
        ).fetchone()

    @staticmethod
    def _validate_state(
        *,
        status: WorkStatus,
        waiting_for: str | None,
        blocked_reason: str | None,
        next_action: str | None,
        completed_at: str | None,
    ) -> None:
        if status == WorkStatus.WAITING and not waiting_for:
            raise ValueError("WAITING requires waiting_for")
        if status == WorkStatus.BLOCKED and not blocked_reason:
            raise ValueError("BLOCKED requires blocked_reason")
        if status == WorkStatus.DONE and (
            not completed_at or waiting_for or blocked_reason or next_action
        ):
            raise ValueError("DONE invariant failed")

    @staticmethod
    def _changed_fields(before: dict, after: dict) -> list[str]:
        tracked = [
            "project_id",
            "title",
            "status",
            "priority",
            "waiting_for",
            "blocked_reason",
            "next_action",
            "last_activity_on",
            "completed_at",
        ]
        return [field for field in tracked if before.get(field) != after.get(field)]

    @staticmethod
    def _insert_audit(
        connection,
        *,
        user_id: str,
        fact_group_id: str,
        receipt_id: str,
        operation: str,
        target_type: str,
        target_id: str,
        expected_version: int | None,
        applied_version: int | None,
        before: dict | None,
        after: dict | None,
        now: str,
    ) -> str:
        audit_id = new_id("audit")
        connection.execute(
            """
            INSERT INTO change_audit(
                id,
                user_id,
                fact_group_id,
                receipt_id,
                operation,
                target_type,
                target_id,
                expected_version,
                applied_version,
                before_json,
                after_json,
                created_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                audit_id,
                user_id,
                fact_group_id,
                receipt_id,
                operation,
                target_type,
                target_id,
                expected_version,
                applied_version,
                canonical_json(before) if before is not None else None,
                canonical_json(after) if after is not None else None,
                now,
            ),
        )
        return audit_id

    @staticmethod
    def _matching_active_project_ids(
        connection,
        *,
        user_id: str,
        project_mention: str,
    ) -> set[str]:
        rows = connection.execute(
            """
            SELECT p.id, p.name, pa.alias
            FROM projects p
            LEFT JOIN project_aliases pa
              ON pa.project_id = p.id
             AND pa.user_id = p.user_id
            WHERE p.user_id = ? AND p.archived_at IS NULL
            """,
            (user_id,),
        ).fetchall()
        return {
            row["id"]
            for row in rows
            if project_mentions_match(project_mention, row["name"])
            or (
                row["alias"] is not None
                and project_mentions_match(project_mention, row["alias"])
            )
        }

    @staticmethod
    def _append_run_event(
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

    @staticmethod
    def _existing_receipt(
        connection,
        *,
        user_id: str,
        fact_group_id: str,
    ) -> ReceiptView | None:
        row = connection.execute(
            """
            SELECT result_json
            FROM change_receipts
            WHERE user_id = ? AND fact_group_id = ?
            """,
            (user_id, fact_group_id),
        ).fetchone()
        return ReceiptView.model_validate_json(row["result_json"]) if row else None

    @staticmethod
    def _recalculate_last_activity(
        connection,
        user_id: str,
        work_item_id: str,
        now: str,
    ) -> None:
        latest = connection.execute(
            """
            SELECT MAX(a.occurred_on_local) AS latest
            FROM activities a
            JOIN activity_links al
              ON al.activity_id = a.id
             AND al.user_id = a.user_id
             AND al.is_active = 1
            WHERE a.user_id = ?
              AND al.work_item_id = ?
              AND a.validity = 'ACTIVE'
            """,
            (user_id, work_item_id),
        ).fetchone()["latest"]
        connection.execute(
            """
            UPDATE work_items
            SET last_activity_on = ?,
                version = version + 1,
                updated_at = ?
            WHERE id = ? AND user_id = ?
            """,
            (latest, now, work_item_id, user_id),
        )

    @staticmethod
    def _refresh_fts(connection, user_id: str, work_item_id: str) -> None:
        row = connection.execute(
            """
            SELECT
                wi.id AS work_item_id,
                wi.title AS work_item_title,
                wi.waiting_for,
                wi.blocked_reason,
                wi.next_action,
                p.id AS project_id,
                p.name AS project_name,
                COALESCE(GROUP_CONCAT(DISTINCT pa.alias), '') AS aliases,
                COALESCE(GROUP_CONCAT(DISTINCT a.summary), '') AS activities
            FROM work_items wi
            JOIN projects p
              ON p.id = wi.project_id
             AND p.user_id = wi.user_id
            LEFT JOIN project_aliases pa
              ON pa.project_id = p.id
             AND pa.user_id = p.user_id
            LEFT JOIN activity_links al
              ON al.work_item_id = wi.id
             AND al.user_id = wi.user_id
             AND al.is_active = 1
            LEFT JOIN activities a
              ON a.id = al.activity_id
             AND a.user_id = al.user_id
             AND a.validity = 'ACTIVE'
            WHERE wi.user_id = ? AND wi.id = ?
            GROUP BY wi.id
            """,
            (user_id, work_item_id),
        ).fetchone()
        connection.execute(
            "DELETE FROM work_memory_fts WHERE work_item_id = ?",
            (work_item_id,),
        )
        if row is None:
            return
        searchable = " ".join(
            filter(
                None,
                [
                    row["waiting_for"],
                    row["blocked_reason"],
                    row["next_action"],
                    row["activities"],
                ],
            )
        )
        connection.execute(
            """
            INSERT INTO work_memory_fts(
                user_id,
                project_id,
                work_item_id,
                project_name,
                project_aliases,
                work_item_title,
                searchable_text
            )
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                user_id,
                row["project_id"],
                row["work_item_id"],
                row["project_name"],
                row["aliases"],
                row["work_item_title"],
                searchable,
            ),
        )
