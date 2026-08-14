#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.database import Database
from app.utils import SystemClock, canonical_json
from app.validation_fixtures import (
    LOCAL_ONLY_WARNING,
    RegressionFixtureError,
    RegressionSourceType,
    export_validation_regression_cases,
    load_validation_regression_cases,
    record_validation_regression,
    summarize_validation_regression_cases,
)


def _read_fixture(path: Path) -> dict:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RegressionFixtureError("input is not a readable JSON fixture") from exc
    if not isinstance(value, dict):
        raise RegressionFixtureError("input fixture must be a JSON object")
    return value


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Manage local-only Phase 1 validation regression fixtures. "
            "Commands never write Canonical Work Memory."
        )
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    record = subparsers.add_parser(
        "record",
        help="atomically record one fixture and its privacy-safe occurrence",
    )
    record.add_argument("--database", required=True, type=Path)
    record.add_argument("--input", required=True, type=Path)
    record.add_argument(
        "--source-type",
        required=True,
        choices=[item.value for item in RegressionSourceType],
    )
    record.add_argument(
        "--source-ref",
        required=True,
        help=(
            "local run/reference/idempotency key; hashed immediately and never "
            "persisted or printed"
        ),
    )
    record.add_argument("--user-id", default="local-user")

    validate = subparsers.add_parser(
        "validate", help="validate every stored generic fixture"
    )
    validate.add_argument("--database", required=True, type=Path)
    validate.add_argument("--user-id", default="local-user")

    export = subparsers.add_parser(
        "export", help="export generic and legacy Context fixtures as JSONL"
    )
    export.add_argument("--database", required=True, type=Path)
    export.add_argument("--user-id", default="local-user")
    export.add_argument(
        "--generic-only",
        action="store_true",
        help="exclude legacy context-link-regression.v1 fixtures",
    )

    summary = subparsers.add_parser(
        "summary", help="print aggregate fixture counts without work content"
    )
    summary.add_argument("--database", required=True, type=Path)
    summary.add_argument("--user-id", default="local-user")
    return parser


def main() -> int:
    parser = _parser()
    arguments = parser.parse_args()
    database_path = arguments.database.expanduser().resolve()
    database = Database(database_path, clock=SystemClock())
    try:
        # Acquire the same shared runtime lock as the FastAPI process before
        # opening SQLite. An offline restore needs the exclusive counterpart,
        # so it can neither replace the file under this command nor leave this
        # command writing to an old inode.
        with database.runtime_lock():
            if arguments.command == "record":
                print(LOCAL_ONLY_WARNING, file=sys.stderr)
                result = record_validation_regression(
                    database,
                    user_id=arguments.user_id,
                    value=_read_fixture(arguments.input),
                    source_type=arguments.source_type,
                    source_ref=arguments.source_ref,
                )
                print(canonical_json(result))
                return 0

            connection = database.connect()
            try:
                if arguments.command == "validate":
                    records = load_validation_regression_cases(
                        connection, user_id=arguments.user_id
                    )
                    print(
                        canonical_json(
                            {"status": "VALID", "count": len(records)}
                        )
                    )
                    return 0

                records = export_validation_regression_cases(
                    connection,
                    user_id=arguments.user_id,
                    include_legacy_context=not getattr(
                        arguments, "generic_only", False
                    ),
                )
                if arguments.command == "summary":
                    print(
                        json.dumps(
                            summarize_validation_regression_cases(records),
                            ensure_ascii=False,
                            indent=2,
                            sort_keys=True,
                        )
                    )
                    return 0

                print(LOCAL_ONLY_WARNING, file=sys.stderr)
                for record in records:
                    print(canonical_json(record))
                return 0
            finally:
                connection.close()
    except RegressionFixtureError as exc:
        # Error messages are intentionally generic and never echo fixture data.
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2
    except sqlite3.Error:
        print("ERROR: could not read or record validation data", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
