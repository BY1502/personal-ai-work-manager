from __future__ import annotations

import json
import math
import sqlite3
from collections import Counter
from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from app.database import Database
from app.stabilization import (
    CORRECTION_EVENT_TYPE,
    load_context_link_regression_cases,
)
from app.utils import canonical_json, new_id, sha256_text, utc_iso


VALIDATION_REGRESSION_SCHEMA_VERSION = "validation-regression.v1"
VALIDATION_REGRESSION_SUMMARY_SCHEMA_VERSION = (
    "validation-regression-summary.v1"
)
LOCAL_ONLY_PRIVACY_MARKER = "LOCAL_ONLY"
LOCAL_ONLY_WARNING = (
    "WARNING: exported validation regression fixtures are LOCAL ONLY and may "
    "contain real work content. Do not commit, upload, or include this output "
    "in aggregate validation reports without review."
)
MAX_FIXTURE_BYTES = 250_000
FORBIDDEN_FIXTURE_KEYS = frozenset(
    {
        "analysis",
        "chainofthought",
        "modelreasoning",
        "rawmodeloutput",
        "rawprovideroutput",
        "reasoning",
        "reasoningcontent",
    }
)


class RegressionFixtureError(ValueError):
    """A safe-to-display error which never renders private fixture input."""


class RegressionCategory(StrEnum):
    PROJECT_MISCLASSIFICATION = "PROJECT_MISCLASSIFICATION"
    DUPLICATE_WORK_ITEM = "DUPLICATE_WORK_ITEM"
    DUPLICATE_ACTIVITY = "DUPLICATE_ACTIVITY"
    STATUS_INCORRECT = "STATUS_INCORRECT"
    NEXT_ACTION_INCORRECT = "NEXT_ACTION_INCORRECT"
    REPORT_CORRECTION_REQUIRED = "REPORT_CORRECTION_REQUIRED"
    CONTEXT_LINK_INCORRECT = "CONTEXT_LINK_INCORRECT"
    EXTRACTION_FAILURE = "EXTRACTION_FAILURE"
    PROVIDER_TIMEOUT = "PROVIDER_TIMEOUT"
    MEMORY_RECOVERY = "MEMORY_RECOVERY"
    SQLITE_RECOVERY = "SQLITE_RECOVERY"
    OTHER = "OTHER"


class RegressionSourceType(StrEnum):
    USER_CORRECTION = "USER_CORRECTION"
    OPERATOR_REVIEW = "OPERATOR_REVIEW"
    AUDIT_FINDING = "AUDIT_FINDING"
    CONTEXT_CORRECTION_EVENT = "CONTEXT_CORRECTION_EVENT"


class RegressionTurn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    role: Literal["USER", "ASSISTANT"]
    content: str = Field(min_length=1, max_length=10_000)


class ValidationRegressionFixture(BaseModel):
    """Local-only, portable reproduction of an observed Phase 1 defect.

    ``setup`` describes the minimum Structured Memory state required before
    replay. ``turns`` contains the user-visible exchange. ``observed`` records
    the faulty outcome and ``expected`` is the assertion target. No timestamp,
    database ID or source occurrence is part of the fixture identity, so the
    same scenario keeps one stable content hash across repeated observations.
    """

    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["validation-regression.v1"] = (
        VALIDATION_REGRESSION_SCHEMA_VERSION
    )
    privacy: Literal["LOCAL_ONLY"] = LOCAL_ONLY_PRIVACY_MARKER
    category: RegressionCategory
    setup: dict[str, Any] = Field(default_factory=dict)
    turns: list[RegressionTurn] = Field(min_length=1, max_length=50)
    observed: dict[str, Any] = Field(min_length=1)
    expected: dict[str, Any] = Field(min_length=1)

    @model_validator(mode="after")
    def require_bounded_json_payload(self) -> "ValidationRegressionFixture":
        payload = self.model_dump(mode="json")
        _require_plain_json(payload)
        try:
            encoded = canonical_json(payload).encode("utf-8")
        except (TypeError, ValueError) as exc:
            raise ValueError("fixture fields must contain JSON values") from exc
        if len(encoded) > MAX_FIXTURE_BYTES:
            raise ValueError("fixture exceeds the local corpus size limit")
        return self


def _require_plain_json(value: Any) -> None:
    """Reject non-JSON objects and non-finite numbers hidden in open maps."""

    if value is None or isinstance(value, (str, bool, int)):
        return
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError("fixture numbers must be finite")
        return
    if isinstance(value, list):
        for item in value:
            _require_plain_json(item)
        return
    if isinstance(value, dict):
        if not all(isinstance(key, str) for key in value):
            raise ValueError("fixture object keys must be strings")
        for key, item in value.items():
            normalized_key = "".join(
                character
                for character in key.casefold()
                if character.isalnum()
            )
            if normalized_key in FORBIDDEN_FIXTURE_KEYS:
                raise ValueError("fixture contains a forbidden diagnostic field")
            _require_plain_json(item)
        return
    raise ValueError("fixture fields must contain plain JSON values")


def _fixture_payload(
    fixture: ValidationRegressionFixture,
) -> dict[str, Any]:
    return fixture.model_dump(mode="json")


def fixture_case_id(fixture: ValidationRegressionFixture) -> str:
    """Return the stable content-addressed ID used for fixture deduplication."""

    return sha256_text(canonical_json(_fixture_payload(fixture)))


def make_validation_regression_record(
    value: dict[str, Any],
) -> dict[str, Any]:
    """Validate a bare fixture or exported record and verify its case ID."""

    supplied_case_id = value.get("case_id")
    fixture_input = {key: item for key, item in value.items() if key != "case_id"}
    try:
        fixture = ValidationRegressionFixture.model_validate(fixture_input)
    except ValidationError as exc:
        raise RegressionFixtureError(
            "fixture does not match validation-regression.v1"
        ) from exc

    expected_case_id = fixture_case_id(fixture)
    if supplied_case_id is not None and supplied_case_id != expected_case_id:
        raise RegressionFixtureError(
            "fixture case_id does not match its validated payload"
        )
    return {"case_id": expected_case_id, **_fixture_payload(fixture)}


def _source_exists(
    connection: sqlite3.Connection,
    *,
    user_id: str,
    source_type: RegressionSourceType,
    source_ref: str,
) -> bool:
    if source_type == RegressionSourceType.CONTEXT_CORRECTION_EVENT:
        row = connection.execute(
            """
            SELECT 1
            FROM execution_events
            WHERE user_id = ? AND run_id = ? AND event_type = ?
            LIMIT 1
            """,
            (user_id, source_ref, CORRECTION_EVENT_TYPE),
        ).fetchone()
        return row is not None
    if source_type == RegressionSourceType.USER_CORRECTION:
        row = connection.execute(
            """
            SELECT 1 FROM (
                SELECT id
                FROM orchestration_runs
                WHERE user_id = ? AND id = ?
                UNION ALL
                SELECT id
                FROM work_fact_groups
                WHERE user_id = ? AND id = ?
                UNION ALL
                SELECT id
                FROM report_snapshots
                WHERE user_id = ? AND id = ?
            )
            LIMIT 1
            """,
            (
                user_id,
                source_ref,
                user_id,
                source_ref,
                user_id,
                source_ref,
            ),
        ).fetchone()
        return row is not None
    # Operator and audit idempotency keys originate outside Canonical Memory,
    # so existence cannot be checked. They are still hashed before persistence.
    return True


def record_validation_regression(
    database: Database,
    *,
    user_id: str,
    value: dict[str, Any],
    source_type: RegressionSourceType | str,
    source_ref: str,
) -> dict[str, Any]:
    """Atomically store a local fixture and a privacy-safe occurrence marker.

    ``source_ref`` may be a run ID, report review key, or operator idempotency
    key. It is used for source validation where possible, hashed immediately,
    and never persisted or returned. Replaying the same category/source pair is
    idempotent while a different source can record another occurrence of the
    same fixture.
    """

    if not isinstance(source_ref, str) or not source_ref.strip():
        raise RegressionFixtureError("source_ref must be a non-empty string")
    if len(source_ref) > 1_000:
        raise RegressionFixtureError("source_ref exceeds the size limit")
    try:
        resolved_source_type = RegressionSourceType(source_type)
    except ValueError as exc:
        raise RegressionFixtureError("unsupported validation source_type") from exc

    record = make_validation_regression_record(value)
    fixture_json = canonical_json(
        {key: item for key, item in record.items() if key != "case_id"}
    )
    source_ref_hash = sha256_text(source_ref)
    now = utc_iso(database.clock.now_utc())

    with database.transaction() as connection:
        if not _source_exists(
            connection,
            user_id=user_id,
            source_type=resolved_source_type,
            source_ref=source_ref,
        ):
            raise RegressionFixtureError(
                "source_ref does not identify an eligible local occurrence"
            )

        existing_finding = connection.execute(
            """
            SELECT id, case_id
            FROM validation_findings
            WHERE user_id = ?
              AND category = ?
              AND source_type = ?
              AND source_ref_hash = ?
            """,
            (
                user_id,
                record["category"],
                resolved_source_type.value,
                source_ref_hash,
            ),
        ).fetchone()
        if (
            existing_finding is not None
            and existing_finding["case_id"] != record["case_id"]
        ):
            raise RegressionFixtureError(
                "source occurrence is already linked to a different fixture"
            )

        existing_fixture = connection.execute(
            """
            SELECT fixture_json
            FROM validation_regression_fixtures
            WHERE user_id = ? AND case_id = ?
            """,
            (user_id, record["case_id"]),
        ).fetchone()
        if existing_fixture is None:
            connection.execute(
                """
                INSERT INTO validation_regression_fixtures(
                    user_id, case_id, fixture_json, created_at
                )
                VALUES (?, ?, ?, ?)
                """,
                (user_id, record["case_id"], fixture_json, now),
            )
            fixture_created = True
        elif existing_fixture["fixture_json"] != fixture_json:
            raise RegressionFixtureError("fixture hash collision detected")
        else:
            fixture_created = False

        if existing_finding is None:
            finding_id = new_id("finding")
            connection.execute(
                """
                INSERT INTO validation_findings(
                    id,
                    user_id,
                    case_id,
                    category,
                    source_type,
                    source_ref_hash,
                    recorded_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    finding_id,
                    user_id,
                    record["case_id"],
                    record["category"],
                    resolved_source_type.value,
                    source_ref_hash,
                    now,
                ),
            )
            finding_created = True
        else:
            finding_id = existing_finding["id"]
            finding_created = False

    return {
        "case_id": record["case_id"],
        "finding_id": finding_id,
        "fixture_created": fixture_created,
        "finding_created": finding_created,
    }


def load_validation_regression_cases(
    connection: sqlite3.Connection,
    *,
    user_id: str = "local-user",
) -> list[dict[str, Any]]:
    """Load validated generic fixtures without exposing occurrence metadata."""

    rows = connection.execute(
        """
        SELECT case_id, fixture_json
        FROM validation_regression_fixtures
        WHERE user_id = ?
        ORDER BY created_at, case_id
        """,
        (user_id,),
    ).fetchall()
    records: list[dict[str, Any]] = []
    for index, row in enumerate(rows, start=1):
        try:
            payload = json.loads(row["fixture_json"])
        except (TypeError, json.JSONDecodeError) as exc:
            raise RegressionFixtureError(
                f"stored fixture {index} contains invalid JSON"
            ) from exc
        if not isinstance(payload, dict):
            raise RegressionFixtureError(
                f"stored fixture {index} is not a JSON object"
            )
        try:
            record = make_validation_regression_record(
                {"case_id": row["case_id"], **payload}
            )
        except RegressionFixtureError as exc:
            raise RegressionFixtureError(
                f"stored fixture {index} failed schema validation"
            ) from exc
        records.append(record)
    return records


def export_validation_regression_cases(
    connection: sqlite3.Connection,
    *,
    user_id: str = "local-user",
    include_legacy_context: bool = True,
) -> list[dict[str, Any]]:
    """Combine legacy Context fixtures and generic local-only fixtures."""

    combined: list[dict[str, Any]] = []
    if include_legacy_context:
        combined.extend(
            load_context_link_regression_cases(connection, user_id=user_id)
        )
    combined.extend(
        load_validation_regression_cases(connection, user_id=user_id)
    )

    deduplicated: list[dict[str, Any]] = []
    seen: set[str] = set()
    for record in combined:
        case_id = record.get("case_id")
        if not isinstance(case_id, str) or case_id in seen:
            continue
        seen.add(case_id)
        deduplicated.append(record)
    return deduplicated


def summarize_validation_regression_cases(
    records: list[dict[str, Any]],
) -> dict[str, Any]:
    """Return an aggregate-only projection safe for a Validation Summary."""

    categories: Counter[str] = Counter()
    legacy_count = 0
    for record in records:
        if record.get("schema_version") == VALIDATION_REGRESSION_SCHEMA_VERSION:
            categories[str(record["category"])] += 1
        else:
            legacy_count += 1
            categories[str(record.get("category", "LEGACY_CONTEXT_LINK"))] += 1
    return {
        "schema_version": VALIDATION_REGRESSION_SUMMARY_SCHEMA_VERSION,
        "total": len(records),
        "legacy_context_fixture_count": legacy_count,
        "by_category": dict(sorted(categories.items())),
    }
