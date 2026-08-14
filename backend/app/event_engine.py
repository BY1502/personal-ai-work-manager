from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date
from typing import Any

from app.database import Database
from app.utils import canonical_json, local_date, new_id, sha256_text, utc_iso


TRIGGER_POLICY_VERSION = "trigger-v1"


@dataclass(frozen=True)
class DomainEvent:
    event_type: str
    aggregate_type: str
    aggregate_id: str | None
    payload: dict[str, Any]


@dataclass(frozen=True)
class TriggerSuggestion:
    id: str
    trigger_type: str
    title: str
    detail: str
    status: str


class EventEngine:
    """Small deterministic event ledger plus Trigger evaluation boundary."""

    def __init__(self, database: Database) -> None:
        self.database = database
        self.triggers = TriggerEngine(database)

    def emit(self, *, user_id: str, event: DomainEvent) -> bool:
        payload = _safe_payload(event.payload)
        source_digest = sha256_text(
            canonical_json(
                {
                    "event_type": event.event_type,
                    "aggregate_type": event.aggregate_type,
                    "aggregate_id": event.aggregate_id,
                    "payload": payload,
                }
            )
        )
        now = utc_iso(self.database.clock.now_utc())
        with self.database.transaction() as connection:
            cursor = connection.execute(
                """
                INSERT OR IGNORE INTO domain_events(
                    id, user_id, event_type, aggregate_type, aggregate_id,
                    source_digest, payload_json, occurred_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    new_id("devent"),
                    user_id,
                    event.event_type,
                    event.aggregate_type,
                    event.aggregate_id,
                    source_digest,
                    canonical_json(payload),
                    now,
                ),
            )
        inserted = cursor.rowcount == 1
        if inserted:
            self.triggers.refresh(user_id=user_id)
        return inserted

    def suggestions(self, *, user_id: str, limit: int = 3) -> list[TriggerSuggestion]:
        self.triggers.refresh(user_id=user_id)
        connection = self.database.connect()
        try:
            rows = connection.execute(
                """
                SELECT id, trigger_type, title, detail, status
                FROM trigger_suggestions
                WHERE user_id = ? AND status = 'ACTIVE'
                ORDER BY updated_at DESC, id
                LIMIT ?
                """,
                (user_id, max(1, min(3, limit))),
            ).fetchall()
            return [TriggerSuggestion(**dict(row)) for row in rows]
        finally:
            connection.close()


class TriggerEngine:
    """Deterministic trigger policy. It never mutates Canonical Memory."""

    def __init__(self, database: Database) -> None:
        self.database = database

    def refresh(self, *, user_id: str) -> None:
        today = local_date(self.database.clock, self.database.timezone_name)
        candidates = self._candidates(user_id=user_id, today=today)
        now = utc_iso(self.database.clock.now_utc())
        candidate_digests = [
            sha256_text(
                canonical_json(
                    {
                        "trigger_type": candidate["trigger_type"],
                        "target_ref": candidate.get("target_ref"),
                        "facts": candidate["facts"],
                        "policy_version": TRIGGER_POLICY_VERSION,
                    }
                )
            )
            for candidate in candidates
        ]
        with self.database.transaction() as connection:
            # A changed status/date produces a new source digest. Retire the
            # previous active suggestion so the API cannot keep recommending a
            # condition that no longer holds. Dismissed rows remain history.
            if candidate_digests:
                placeholders = ", ".join("?" for _ in candidate_digests)
                connection.execute(
                    f"""
                    UPDATE trigger_suggestions
                    SET status = 'EXPIRED', updated_at = ?
                    WHERE user_id = ? AND status = 'ACTIVE'
                      AND policy_version = ?
                      AND source_digest NOT IN ({placeholders})
                    """,
                    (now, user_id, TRIGGER_POLICY_VERSION, *candidate_digests),
                )
            else:
                connection.execute(
                    """
                    UPDATE trigger_suggestions
                    SET status = 'EXPIRED', updated_at = ?
                    WHERE user_id = ? AND status = 'ACTIVE'
                      AND policy_version = ?
                    """,
                    (now, user_id, TRIGGER_POLICY_VERSION),
                )
            for candidate, source_digest in zip(candidates, candidate_digests):
                connection.execute(
                    """
                    INSERT INTO trigger_suggestions(
                        id, user_id, trigger_type, title, detail,
                        target_type, target_ref, source_digest, policy_version,
                        status, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'ACTIVE', ?, ?)
                    ON CONFLICT(user_id, trigger_type, source_digest, policy_version)
                    DO UPDATE SET title = excluded.title,
                                  detail = excluded.detail,
                                  updated_at = excluded.updated_at,
                                  status = CASE
                                      WHEN trigger_suggestions.status = 'DISMISSED'
                                      THEN 'DISMISSED'
                                      ELSE 'ACTIVE'
                                  END
                    """,
                    (
                        new_id("suggest"),
                        user_id,
                        candidate["trigger_type"],
                        candidate["title"],
                        candidate["detail"],
                        candidate.get("target_type"),
                        candidate.get("target_ref"),
                        source_digest,
                        TRIGGER_POLICY_VERSION,
                        now,
                        now,
                    ),
                )

    def _candidates(self, *, user_id: str, today: date) -> list[dict[str, Any]]:
        connection = self.database.connect()
        try:
            rows = connection.execute(
                """
                SELECT wi.id, wi.title, wi.status, wi.priority,
                       wi.waiting_for, wi.blocked_reason, wi.next_action,
                       wi.last_activity_on, wi.updated_at,
                       p.name AS project_name
                FROM work_items wi
                JOIN projects p ON p.id = wi.project_id AND p.user_id = wi.user_id
                WHERE wi.user_id = ?
                  AND wi.archived_at IS NULL AND p.archived_at IS NULL
                  AND wi.status NOT IN ('DONE', 'HOLD')
                ORDER BY wi.id
                """,
                (user_id,),
            ).fetchall()
        finally:
            connection.close()

        candidates: list[dict[str, Any]] = []
        for row in rows:
            last_date = _row_date(row["last_activity_on"] or row["updated_at"][:10])
            age_days = max(0, (today - last_date).days)
            common = {
                "target_type": "WORK_ITEM",
                "target_ref": row["id"],
                "project_name": row["project_name"],
                "work_title": row["title"],
                "age_days": age_days,
                "status": row["status"],
            }
            if row["status"] == "WAITING" and age_days >= 3:
                candidates.append(
                    {
                        "trigger_type": "WAITING_TOO_LONG",
                        "title": f"{row['project_name']} 업무의 회신을 확인할 때입니다.",
                        "detail": f"‘{row['title']}’가 {age_days}일째 회신 대기 중입니다.",
                        "facts": common | {"waiting_for": row["waiting_for"]},
                        **common,
                    }
                )
            elif row["status"] == "BLOCKED" and age_days >= 3:
                candidates.append(
                    {
                        "trigger_type": "BLOCKED_TOO_LONG",
                        "title": f"{row['project_name']} 업무의 막힌 원인을 확인할 때입니다.",
                        "detail": f"‘{row['title']}’가 {age_days}일째 막혀 있습니다.",
                        "facts": common | {"blocked_reason": row["blocked_reason"]},
                        **common,
                    }
                )
            elif row["priority"] == "HIGH" and age_days >= 3:
                candidates.append(
                    {
                        "trigger_type": "HIGH_PRIORITY_IDLE",
                        "title": f"우선순위가 높은 {row['project_name']} 업무를 다시 확인하세요.",
                        "detail": f"‘{row['title']}’에 {age_days}일 동안 새 활동이 없습니다.",
                        "facts": common | {"priority": "HIGH"},
                        **common,
                    }
                )
            elif row["status"] in {"IN_PROGRESS", "TODO"} and age_days >= 14:
                candidates.append(
                    {
                        "trigger_type": "WORK_STALE",
                        "title": f"{row['project_name']} 업무가 오래 멈춰 있습니다.",
                        "detail": f"‘{row['title']}’에 {age_days}일 동안 새 활동이 없습니다.",
                        "facts": common,
                        **common,
                    }
                )
        candidates.sort(key=lambda item: (-int(item["facts"]["age_days"]), item["trigger_type"], item["target_ref"]))
        return candidates[:3]


def _row_date(value: str) -> date:
    try:
        return date.fromisoformat(value[:10])
    except (TypeError, ValueError):
        return date.min


def _safe_payload(payload: dict[str, Any]) -> dict[str, Any]:
    """Keep the event ledger structured and bounded; never accept raw text."""
    encoded = json.loads(canonical_json(payload))
    if not isinstance(encoded, dict):
        raise ValueError("event payload must be an object")
    if len(canonical_json(encoded).encode("utf-8")) > 20_000:
        raise ValueError("event payload is too large")
    return encoded
