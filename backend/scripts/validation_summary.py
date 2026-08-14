#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import date
from pathlib import Path

from app.database import Database
from app.utils import SystemClock
from app.validation_metrics import ValidationPeriodError, ValidationSummaryService


def _date(value: str) -> date:
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("date must use YYYY-MM-DD") from exc


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Generate a read-only, privacy-safe Phase 1 validation summary."
        )
    )
    parser.add_argument(
        "--database",
        type=Path,
        default=Path(
            os.environ.get("PERSONAL_AI_DB_PATH", "data/personal_ai.db")
        ),
    )
    parser.add_argument("--user-id", default="local-user")
    period = parser.add_mutually_exclusive_group()
    period.add_argument(
        "--week-containing",
        type=_date,
        help="Local date whose Monday-Sunday week should be summarized.",
    )
    period.add_argument(
        "--start",
        type=_date,
        help="Inclusive local start date; --end is also required.",
    )
    parser.add_argument(
        "--end",
        type=_date,
        help="Inclusive local end date; --start is also required.",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = _parser()
    arguments = parser.parse_args(argv)
    if (arguments.start is None) != (arguments.end is None):
        parser.error("--start and --end must be provided together")
    if arguments.week_containing is not None and arguments.end is not None:
        parser.error("--week-containing cannot be combined with --end")
    database_path = arguments.database.expanduser().resolve()
    if not database_path.is_file():
        parser.error("database file does not exist")

    database = Database(database_path, clock=SystemClock())
    try:
        with database.runtime_lock():
            summary = ValidationSummaryService(database).summarize(
                user_id=arguments.user_id,
                week_containing=arguments.week_containing,
                start_local=arguments.start,
                end_local=arguments.end,
            )
    except ValidationPeriodError as exc:
        parser.error(str(exc))
    json.dump(
        summary.model_dump(mode="json"),
        sys.stdout,
        ensure_ascii=False,
        sort_keys=True,
        indent=2,
    )
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
