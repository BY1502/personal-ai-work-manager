#!/usr/bin/env python3
from __future__ import annotations

import argparse
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.stabilization import load_context_link_regression_cases
from app.utils import canonical_json


def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Export user-reported Context Linking corrections as stable JSONL "
            "regression fixtures."
        )
    )
    parser.add_argument("database", type=Path, help="SQLite database path")
    parser.add_argument("--user-id", default="local-user")
    args = parser.parse_args()

    connection = sqlite3.connect(args.database)
    connection.row_factory = sqlite3.Row
    try:
        for case in load_context_link_regression_cases(
            connection,
            user_id=args.user_id,
        ):
            print(canonical_json(case))
    finally:
        connection.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
