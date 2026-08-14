from __future__ import annotations

import json
import re
import sqlite3
from dataclasses import dataclass
from typing import Any

from app.utils import canonical_json, normalize_name, sha256_text


CORRECTION_EVENT_TYPE = "CONTEXT_LINK_CORRECTION_REPORTED"
CORRECTION_PROGRESS_EVENT_TYPE = "CONTEXT_LINK_CORRECTION_PROGRESS"
CORRECTION_SCHEMA_VERSION = "context-link-correction.v1"
REGRESSION_SCHEMA_VERSION = "context-link-regression.v1"


@dataclass(frozen=True)
class ContextLinkCorrectionSignal:
    rejected_project_mention: str
    expected_project_mention: str
    source_text: str


def detect_context_link_correction(
    content: str,
) -> ContextLinkCorrectionSignal | None:
    """Recognize an explicit Korean project reassignment correction.

    This is deliberately narrow. A sentence must begin as a correction and
    contain an explicit ``A 말고/아니고 B`` contrast. Everything else remains
    in the normal extraction flow so this stability hook cannot silently turn
    ordinary work statements into corrections.
    """

    text = content.strip()
    if not re.match(r"^(?:아니(?:야)?|정정(?:할게|합니다)?|그거|그건)\b", text):
        return None

    contrast = re.search(r"\s+(?:말고|아니고)\s+", text)
    if contrast is None:
        return None

    rejected = text[: contrast.start()].strip(" \t,.")
    expected = text[contrast.end() :].strip(" \t,.!?")
    rejected = re.sub(
        r"^(?:아니(?:야)?|정정(?:할게|합니다)?)\s*[,\s]*", "", rejected
    )
    rejected = re.sub(r"^(?:그거|그건|이거|이건)\s*", "", rejected)
    expected = re.sub(
        r"\s*쪽(?:이야|이었어|이에요|예요|입니다|였어|였어요)?$", "", expected
    )
    expected = re.sub(
        r"(?:이야|이에요|예요|입니다|였어|였어요)$", "", expected
    ).strip()

    if not rejected or not expected:
        return None
    if len(rejected) > 200 or len(expected) > 200:
        return None
    return ContextLinkCorrectionSignal(
        rejected_project_mention=rejected,
        expected_project_mention=expected,
        source_text=text,
    )


def project_mentions_match(left: str, right: str) -> bool:
    """Allow a specific project name to match its unambiguous short form."""

    normalized_left = normalize_name(left)
    normalized_right = normalize_name(right)
    return bool(
        normalized_left
        and normalized_right
        and (
            normalized_left == normalized_right
            or normalized_left in normalized_right
            or normalized_right in normalized_left
        )
    )


def make_regression_case(
    *,
    signal: ContextLinkCorrectionSignal,
    original_content: str | None,
    observed_project_name: str | None,
    observed_work_item_title: str | None,
    observed_link_decision: str | None,
    canonical_patch_review_required: bool,
) -> tuple[str, dict[str, Any]]:
    turns = []
    if original_content:
        turns.append({"role": "USER", "content": original_content})
    turns.append({"role": "USER", "content": signal.source_text})
    regression_case: dict[str, Any] = {
        "schema_version": REGRESSION_SCHEMA_VERSION,
        "category": "PROJECT_MISCLASSIFICATION",
        "turns": turns,
        "observed": {
            "project_name": observed_project_name,
            "work_item_title": observed_work_item_title,
            "link_decision": observed_link_decision,
        },
        "expected": {
            "project_mention": signal.expected_project_mention,
            "must_not_select_project": (
                observed_project_name or signal.rejected_project_mention
            ),
            "canonical_patch_review_required": (
                canonical_patch_review_required
            ),
        },
    }
    return sha256_text(canonical_json(regression_case)), regression_case


def load_context_link_regression_cases(
    connection: sqlite3.Connection,
    *,
    user_id: str = "local-user",
) -> list[dict[str, Any]]:
    """Return deduplicated, stable fixtures recorded from real corrections."""

    rows = connection.execute(
        """
        SELECT payload_json
        FROM execution_events
        WHERE user_id = ? AND event_type = ?
        ORDER BY created_at, sequence
        """,
        (user_id, CORRECTION_EVENT_TYPE),
    ).fetchall()
    cases: list[dict[str, Any]] = []
    seen: set[str] = set()
    for row in rows:
        try:
            payload = json.loads(row["payload_json"])
        except (TypeError, json.JSONDecodeError):
            continue
        if payload.get("schema_version") != CORRECTION_SCHEMA_VERSION:
            continue
        case_id = payload.get("case_id")
        regression_case = payload.get("regression_case")
        if (
            not isinstance(case_id, str)
            or case_id in seen
            or not isinstance(regression_case, dict)
            or regression_case.get("schema_version")
            != REGRESSION_SCHEMA_VERSION
        ):
            continue
        seen.add(case_id)
        cases.append({"case_id": case_id, **regression_case})
    return cases
