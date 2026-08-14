#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from app.database import Database
from app.stability import StabilityAuditService
from app.utils import SystemClock


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Run read-only Phase 1 stability checks against a SQLite DB."
    )
    parser.add_argument(
        "--db",
        type=Path,
        default=Path(os.getenv("PERSONAL_AI_DB_PATH", "data/personal_ai.db")),
        help="SQLite database path (defaults to PERSONAL_AI_DB_PATH).",
    )
    parser.add_argument("--user-id", default="local-user")
    parser.add_argument(
        "--fail-on-attention",
        action="store_true",
        help="Exit with status 1 when an invariant check needs attention.",
    )
    arguments = parser.parse_args()
    database_path = arguments.db.expanduser().resolve()
    if not database_path.is_file():
        parser.error(f"database does not exist: {database_path}")

    database = Database(database_path, clock=SystemClock())
    report = StabilityAuditService(database).inspect(user_id=arguments.user_id)
    print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
    if arguments.fail_on_attention and report["health"] != "PASS":
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
