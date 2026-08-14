from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, field
from datetime import date, timedelta

from app.database import Database
from app.models import (
    CandidateView,
    LinkDecisionType,
    ValidatedFactGroup,
    WorkStatus,
)
from app.utils import normalize_name, topic_tokens


@dataclass
class CandidateRecord:
    project_id: str
    project_name: str
    project_normalized_name: str
    work_item_id: str
    work_item_title: str
    work_item_normalized_title: str
    status: str
    waiting_for: str | None
    next_action: str | None
    version: int
    searchable_text: str
    sources: set[str] = field(default_factory=set)
    score: float = 0.0
    evidence: list[str] = field(default_factory=list)
    strong_anchor: bool = False

    def to_view(self) -> CandidateView:
        return CandidateView(
            project_id=self.project_id,
            project_name=self.project_name,
            work_item_id=self.work_item_id,
            work_item_title=self.work_item_title,
            status=WorkStatus(self.status),
            waiting_for=self.waiting_for,
            next_action=self.next_action,
            version=self.version,
            score=round(self.score, 3),
            evidence=self.evidence,
        )


@dataclass
class LinkDecision:
    decision: LinkDecisionType
    selected: CandidateRecord | None
    candidates: list[CandidateRecord]
    score: float | None
    margin: float | None
    evidence: list[str]

    def as_json(self) -> str:
        return json.dumps(
            {
                "decision": self.decision.value,
                "selected_work_item_id": (
                    self.selected.work_item_id if self.selected else None
                ),
                "score": self.score,
                "margin": self.margin,
                "evidence": self.evidence,
                "candidates": [
                    candidate.to_view().model_dump(mode="json")
                    for candidate in self.candidates
                ],
                "policy_version": "context-link-v1",
            },
            ensure_ascii=False,
            sort_keys=True,
        )

    @classmethod
    def from_json(cls, payload: str) -> "LinkDecision":
        raw = json.loads(payload)
        candidates = [
            CandidateRecord(
                project_id=item["project_id"],
                project_name=item["project_name"],
                project_normalized_name=normalize_name(item["project_name"]),
                work_item_id=item["work_item_id"],
                work_item_title=item["work_item_title"],
                work_item_normalized_title=normalize_name(item["work_item_title"]),
                status=item["status"],
                waiting_for=item.get("waiting_for"),
                next_action=item.get("next_action"),
                version=item["version"],
                searchable_text="",
                score=float(item["score"]),
                evidence=list(item.get("evidence", [])),
                strong_anchor=True,
            )
            for item in raw.get("candidates", [])
        ]
        selected_id = raw.get("selected_work_item_id")
        selected = next(
            (item for item in candidates if item.work_item_id == selected_id),
            None,
        )
        return cls(
            decision=LinkDecisionType(raw["decision"]),
            selected=selected,
            candidates=candidates,
            score=raw.get("score"),
            margin=raw.get("margin"),
            evidence=list(raw.get("evidence", [])),
        )


class MemoryManager:
    def __init__(self, database: Database) -> None:
        self.database = database

    def decide_link(
        self,
        *,
        user_id: str,
        conversation_id: str,
        group: ValidatedFactGroup,
        today: date,
    ) -> LinkDecision:
        candidates = self._collect_candidates(
            user_id=user_id,
            conversation_id=conversation_id,
            group=group,
            today=today,
        )
        response_event = any(
            activity.kind.value == "RESPONSE_RECEIVED"
            for activity in group.activities
        )
        recent_waiting = [
            candidate
            for candidate in candidates
            if "RECENT_WAITING_REQUEST" in candidate.sources
        ]
        unique_waiting = response_event and len(recent_waiting) == 1

        for candidate in candidates:
            self._score_candidate(
                candidate,
                group=group,
                unique_waiting=(
                    unique_waiting
                    and candidate.work_item_id == recent_waiting[0].work_item_id
                ),
                competing_waiting_count=len(recent_waiting),
            )

        candidates.sort(key=lambda candidate: (-candidate.score, candidate.work_item_id))
        top = candidates[0] if candidates else None
        runner_up_score = candidates[1].score if len(candidates) > 1 else 0.0
        margin = (top.score - runner_up_score) if top else None

        if top and top.score >= 0.90 and (margin or 0.0) >= 0.20 and top.strong_anchor:
            return LinkDecision(
                decision=LinkDecisionType.AUTO_LINK,
                selected=top,
                candidates=candidates[:5],
                score=top.score,
                margin=margin,
                evidence=top.evidence,
            )

        self_contained = bool(group.project_mention and group.work_item_mention)
        if self_contained and (top is None or top.score < 0.65):
            return LinkDecision(
                decision=LinkDecisionType.CREATE_NEW,
                selected=None,
                candidates=candidates[:5],
                score=top.score if top else None,
                margin=margin,
                evidence=["SELF_CONTAINED_NEW_WORK"],
            )

        if candidates:
            return LinkDecision(
                decision=LinkDecisionType.NEEDS_CLARIFICATION,
                selected=None,
                candidates=candidates[:5],
                score=top.score if top else None,
                margin=margin,
                evidence=["AMBIGUOUS_CANDIDATES"],
            )

        return LinkDecision(
            decision=LinkDecisionType.UNRESOLVED,
            selected=None,
            candidates=[],
            score=None,
            margin=None,
            evidence=["NO_SAFE_TARGET"],
        )

    def project_status(self, *, user_id: str, project_mention: str) -> dict | None:
        normalized = normalize_name(project_mention)
        connection = self.database.connect()
        try:
            project = connection.execute(
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
            if project is None:
                return None

            items = connection.execute(
                """
                SELECT *
                FROM work_items
                WHERE user_id = ?
                  AND project_id = ?
                  AND archived_at IS NULL
                ORDER BY
                  CASE status
                    WHEN 'IN_PROGRESS' THEN 1
                    WHEN 'WAITING' THEN 2
                    WHEN 'BLOCKED' THEN 3
                    WHEN 'TODO' THEN 4
                    WHEN 'HOLD' THEN 5
                    ELSE 6
                  END,
                  updated_at DESC
                """,
                (user_id, project["id"]),
            ).fetchall()
            work_items = [
                self._work_item_detail(connection, user_id, item["id"])
                for item in items
            ]
            return {
                "project_id": project["id"],
                "project_name": project["name"],
                "work_items": work_items,
            }
        finally:
            connection.close()

    def today_activities(self, *, user_id: str, local_day: date) -> list[dict]:
        connection = self.database.connect()
        try:
            rows = connection.execute(
                """
                SELECT
                    a.id,
                    a.kind,
                    a.summary,
                    a.occurred_on_local,
                    p.id AS project_id,
                    p.name AS project_name,
                    wi.id AS work_item_id,
                    wi.title AS work_item_title
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
                  AND a.occurred_on_local = ?
                  AND a.validity = 'ACTIVE'
                ORDER BY a.recorded_at_utc, m.server_sequence, a.claim_sequence
                """,
                (user_id, local_day.isoformat()),
            ).fetchall()
            return [dict(row) for row in rows]
        finally:
            connection.close()

    def get_work_item(self, *, user_id: str, work_item_id: str) -> dict | None:
        connection = self.database.connect()
        try:
            row = connection.execute(
                """
                SELECT wi.*, p.name AS project_name
                FROM work_items wi
                JOIN projects p
                  ON p.id = wi.project_id
                 AND p.user_id = wi.user_id
                WHERE wi.user_id = ? AND wi.id = ?
                """,
                (user_id, work_item_id),
            ).fetchone()
            return dict(row) if row else None
        finally:
            connection.close()

    def _collect_candidates(
        self,
        *,
        user_id: str,
        conversation_id: str,
        group: ValidatedFactGroup,
        today: date,
    ) -> list[CandidateRecord]:
        candidate_map: dict[str, CandidateRecord] = {}
        connection = self.database.connect()
        try:
            if group.project_mention:
                normalized_project = normalize_name(group.project_mention)
                rows = connection.execute(
                    """
                    SELECT DISTINCT wi.id
                    FROM work_items wi
                    JOIN projects p
                      ON p.id = wi.project_id
                     AND p.user_id = wi.user_id
                    LEFT JOIN project_aliases pa ON pa.project_id = p.id
                    WHERE wi.user_id = ?
                      AND wi.archived_at IS NULL
                      AND p.archived_at IS NULL
                      AND (
                        p.normalized_name = ?
                        OR pa.normalized_alias = ?
                      )
                    """,
                    (user_id, normalized_project, normalized_project),
                ).fetchall()
                for row in rows:
                    self._merge_candidate(
                        connection,
                        candidate_map,
                        user_id,
                        row["id"],
                        "EXPLICIT_PROJECT",
                    )
            else:
                focus = connection.execute(
                    """
                    SELECT wfg.target_work_item_id, m.server_sequence
                    FROM work_fact_groups wfg
                    JOIN orchestration_runs r ON r.id = wfg.run_id
                    JOIN chat_messages m ON m.id = wfg.source_message_id
                    WHERE wfg.user_id = ?
                      AND r.conversation_id = ?
                      AND wfg.status = 'APPLIED'
                      AND wfg.target_work_item_id IS NOT NULL
                    ORDER BY m.server_sequence DESC
                    LIMIT 1
                    """,
                    (user_id, conversation_id),
                ).fetchone()
                if focus:
                    current_sequence = connection.execute(
                        """
                        SELECT MAX(server_sequence) AS server_sequence
                        FROM chat_messages
                        WHERE user_id = ?
                          AND conversation_id = ?
                          AND role = 'USER'
                        """,
                        (user_id, conversation_id),
                    ).fetchone()["server_sequence"]
                    self._merge_candidate(
                        connection,
                        candidate_map,
                        user_id,
                        focus["target_work_item_id"],
                        "CONVERSATION_FOCUS",
                    )
                    if current_sequence == focus["server_sequence"] + 1:
                        self._merge_candidate(
                            connection,
                            candidate_map,
                            user_id,
                            focus["target_work_item_id"],
                            "IMMEDIATE_CONVERSATION_FOCUS",
                        )

                since = (today - timedelta(days=7)).isoformat()
                rows = connection.execute(
                    """
                    SELECT DISTINCT wi.id
                    FROM work_items wi
                    JOIN activity_links al
                      ON al.work_item_id = wi.id
                     AND al.user_id = wi.user_id
                     AND al.is_active = 1
                    JOIN activities a
                      ON a.id = al.activity_id
                     AND a.user_id = al.user_id
                    WHERE wi.user_id = ?
                      AND wi.status = 'WAITING'
                      AND wi.archived_at IS NULL
                      AND a.kind = 'REQUEST_SENT'
                      AND a.validity = 'ACTIVE'
                      AND a.occurred_on_local >= ?
                    """,
                    (user_id, since),
                ).fetchall()
                for row in rows:
                    self._merge_candidate(
                        connection,
                        candidate_map,
                        user_id,
                        row["id"],
                        "RECENT_WAITING_REQUEST",
                    )

            fts_tokens = topic_tokens(
                " ".join(
                    [
                        group.project_mention or "",
                        group.work_item_mention or "",
                        group.source_excerpt,
                        *group.reference_terms,
                    ]
                )
            )
            if fts_tokens:
                fts_query = " OR ".join(
                    f'"{token.replace(chr(34), "")}"'
                    for token in sorted(fts_tokens)
                )
                try:
                    rows = connection.execute(
                        """
                        SELECT work_item_id
                        FROM work_memory_fts
                        WHERE work_memory_fts MATCH ?
                          AND user_id = ?
                        ORDER BY bm25(work_memory_fts)
                        LIMIT 5
                        """,
                        (fts_query, user_id),
                    ).fetchall()
                except sqlite3.OperationalError:
                    rows = []
                for row in rows:
                    self._merge_candidate(
                        connection,
                        candidate_map,
                        user_id,
                        row["work_item_id"],
                        "FTS_MATCH",
                    )
        finally:
            connection.close()

        if group.project_mention:
            normalized_project = normalize_name(group.project_mention)
            return [
                candidate
                for candidate in candidate_map.values()
                if candidate.project_normalized_name == normalized_project
            ]
        return list(candidate_map.values())

    def _merge_candidate(
        self,
        connection,
        candidates: dict[str, CandidateRecord],
        user_id: str,
        work_item_id: str,
        source: str,
    ) -> None:
        if work_item_id in candidates:
            candidates[work_item_id].sources.add(source)
            return
        row = connection.execute(
            """
            SELECT
                wi.id AS work_item_id,
                wi.title AS work_item_title,
                wi.normalized_title AS work_item_normalized_title,
                wi.status,
                wi.waiting_for,
                wi.next_action,
                wi.version,
                p.id AS project_id,
                p.name AS project_name,
                p.normalized_name AS project_normalized_name,
                COALESCE(GROUP_CONCAT(a.summary, ' '), '') AS activity_text
            FROM work_items wi
            JOIN projects p
              ON p.id = wi.project_id
             AND p.user_id = wi.user_id
            LEFT JOIN activity_links al
              ON al.work_item_id = wi.id
             AND al.user_id = wi.user_id
             AND al.is_active = 1
            LEFT JOIN activities a
              ON a.id = al.activity_id
             AND a.user_id = al.user_id
             AND a.validity = 'ACTIVE'
            WHERE wi.user_id = ?
              AND wi.id = ?
              AND wi.archived_at IS NULL
              AND p.archived_at IS NULL
            GROUP BY wi.id
            """,
            (user_id, work_item_id),
        ).fetchone()
        if row is None:
            return
        searchable = " ".join(
            filter(
                None,
                [
                    row["project_name"],
                    row["work_item_title"],
                    row["waiting_for"],
                    row["next_action"],
                    row["activity_text"],
                ],
            )
        )
        candidates[work_item_id] = CandidateRecord(
            project_id=row["project_id"],
            project_name=row["project_name"],
            project_normalized_name=row["project_normalized_name"],
            work_item_id=row["work_item_id"],
            work_item_title=row["work_item_title"],
            work_item_normalized_title=row["work_item_normalized_title"],
            status=row["status"],
            waiting_for=row["waiting_for"],
            next_action=row["next_action"],
            version=row["version"],
            searchable_text=searchable,
            sources={source},
        )

    def _score_candidate(
        self,
        candidate: CandidateRecord,
        *,
        group: ValidatedFactGroup,
        unique_waiting: bool,
        competing_waiting_count: int,
    ) -> None:
        score = 0.0
        evidence: list[str] = []
        strong = False

        if group.project_mention and normalize_name(group.project_mention) == (
            candidate.project_normalized_name
        ):
            score += 0.25
            evidence.append("PROJECT_EXACT")

        if group.work_item_mention and normalize_name(group.work_item_mention) == (
            candidate.work_item_normalized_title
        ):
            score += 0.60
            evidence.append("WORK_ITEM_EXACT")
            strong = True

        if "CONVERSATION_FOCUS" in candidate.sources:
            score += 0.25
            evidence.append("CONFIRMED_CONVERSATION_FOCUS")
            if competing_waiting_count <= 1 and group.reference_terms:
                strong = True

        immediate_request_continuation = (
            "IMMEDIATE_CONVERSATION_FOCUS" in candidate.sources
            and candidate.status == "IN_PROGRESS"
            and competing_waiting_count == 0
            and any(
                activity.kind.value == "REQUEST_SENT"
                for activity in group.activities
            )
        )
        if immediate_request_continuation:
            score += 0.65
            evidence.append("IMMEDIATE_REQUEST_CONTINUATION")
            strong = True

        if "RECENT_WAITING_REQUEST" in candidate.sources:
            score += 0.35
            evidence.append("WAITING_RESPONSE_COMPATIBLE")

        if unique_waiting:
            score += 0.35
            evidence.append("UNIQUE_RECENT_WAITING_REFERENT")
            strong = True

        if any(term in {"오늘", "어제", "아까"} for term in group.reference_terms):
            score += 0.10
            evidence.append("TEMPORAL_REFERENCE")

        if "FTS_MATCH" in candidate.sources:
            score += 0.15
            evidence.append("FTS_MATCH")

        overlap = topic_tokens(group.source_excerpt) & topic_tokens(
            candidate.searchable_text
        )
        if overlap:
            score += min(0.20, 0.08 * len(overlap))
            evidence.append("TOPIC_MATCH:" + ",".join(sorted(overlap)))

        candidate.score = min(1.0, score)
        candidate.evidence = evidence
        candidate.strong_anchor = strong

    @staticmethod
    def _work_item_detail(connection, user_id: str, work_item_id: str) -> dict:
        item = connection.execute(
            """
            SELECT *
            FROM work_items
            WHERE user_id = ? AND id = ?
            """,
            (user_id, work_item_id),
        ).fetchone()
        activities = connection.execute(
            """
            SELECT
                a.id,
                a.kind,
                a.summary,
                a.occurred_on_local,
                a.recorded_at_utc
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
                     a.claim_sequence
            """,
            (user_id, work_item_id),
        ).fetchall()
        detail = dict(item)
        detail["activities"] = [dict(activity) for activity in activities]
        return detail
