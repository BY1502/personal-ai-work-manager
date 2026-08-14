from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from app.sqlite_lock import database_file_lock
from app.utils import Clock, canonical_json, new_id, utc_iso


class Database:
    def __init__(
        self,
        path: Path,
        *,
        clock: Clock,
        default_user_id: str = "local-user",
        timezone_name: str = "Asia/Seoul",
    ) -> None:
        self.path = path
        self.clock = clock
        self.default_user_id = default_user_id
        self.timezone_name = timezone_name
        self.migrations_path = Path(__file__).parent / "migrations"

    def connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(
            self.path,
            timeout=5,
            isolation_level=None,
            check_same_thread=False,
        )
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 5000")
        return connection

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        connection = self.connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            yield connection
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    @contextmanager
    def runtime_lock(self) -> Iterator[None]:
        """Prevent an offline restore from replacing a live database."""

        with database_file_lock(
            self.path,
            exclusive=False,
            blocking=True,
        ):
            yield

    def initialize(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        connection = self.connect()
        try:
            connection.execute("PRAGMA journal_mode = WAL")
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS schema_migrations (
                    version TEXT PRIMARY KEY,
                    applied_at TEXT NOT NULL
                )
                """
            )
            applied = {
                row["version"]
                for row in connection.execute(
                    "SELECT version FROM schema_migrations"
                ).fetchall()
            }
            for migration in sorted(self.migrations_path.glob("*.sql")):
                version = migration.stem.split("_", 1)[0]
                if version in applied:
                    continue
                script = migration.read_text(encoding="utf-8")
                applied_at = utc_iso(self.clock.now_utc())
                connection.executescript(
                    "BEGIN IMMEDIATE;\n"
                    + script
                    + "\n"
                    + "INSERT INTO schema_migrations(version, applied_at) "
                    + f"VALUES ('{version}', '{applied_at}');\n"
                    + "COMMIT;"
                )
            now = utc_iso(self.clock.now_utc())
            connection.execute(
                """
                INSERT OR IGNORE INTO users(id, timezone, locale, created_at)
                VALUES (?, ?, 'ko-KR', ?)
                """,
                (self.default_user_id, self.timezone_name, now),
            )
            self._recover_interrupted_runs(connection, now=now)
            connection.execute("PRAGMA optimize")
        finally:
            connection.close()

    @staticmethod
    def _recover_interrupted_runs(
        connection: sqlite3.Connection,
        *,
        now: str,
    ) -> None:
        """Mark abandoned executions retryable and leave a durable audit event.

        SQLite has already rolled back any uncommitted canonical mutation after a
        process stop. The status transition and its diagnostic event are committed
        together so operations can distinguish a restart from a Provider failure.
        Re-running initialization is idempotent because interrupted runs no longer
        match the source status set.
        """

        rows = connection.execute(
            """
            SELECT id, user_id, status
            FROM orchestration_runs
            WHERE result_json IS NULL
              AND status IN ('RECEIVED', 'INTERPRETING', 'PLANNED', 'APPLYING')
            ORDER BY started_at, id
            """
        ).fetchall()
        if not rows:
            return

        connection.execute("BEGIN IMMEDIATE")
        try:
            for row in rows:
                updated = connection.execute(
                    """
                    UPDATE orchestration_runs
                    SET status = 'INTERRUPTED_RETRYABLE',
                        error_code = 'PROCESS_INTERRUPTED'
                    WHERE id = ?
                      AND user_id = ?
                      AND result_json IS NULL
                      AND status = ?
                    """,
                    (row["id"], row["user_id"], row["status"]),
                )
                if updated.rowcount != 1:
                    continue
                sequence = connection.execute(
                    """
                    SELECT COALESCE(MAX(sequence), 0) + 1 AS next_sequence
                    FROM execution_events
                    WHERE run_id = ?
                    """,
                    (row["id"],),
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
                    VALUES (?, ?, ?, ?, 'RUN_INTERRUPTED', ?, ?, ?)
                    """,
                    (
                        new_id("evt"),
                        row["user_id"],
                        row["id"],
                        sequence,
                        "서버 재시작으로 중단된 요청을 재시도 가능 상태로 복구했습니다.",
                        canonical_json(
                            {
                                "previous_status": row["status"],
                                "recovery_action": "MARK_RETRYABLE",
                            }
                        ),
                        now,
                    ),
                )
            connection.commit()
        except Exception:
            connection.rollback()
            raise

    def create_ephemeral_id(self) -> str:
        return new_id("db")
