from __future__ import annotations

import argparse
import hashlib
import json
import os
import sqlite3
import sys
import tempfile
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Sequence

from app.sqlite_lock import DatabaseLockUnavailable, database_file_lock


REQUIRED_TABLES = frozenset(
    {
        "schema_migrations",
        "users",
        "orchestration_runs",
        "projects",
        "work_items",
        "activities",
    }
)


class SQLiteMaintenanceError(RuntimeError):
    """A safe backup, verification, or restore could not be completed."""


@dataclass(frozen=True)
class DatabaseVerification:
    database_path: str
    size_bytes: int
    page_count: int
    migrations: tuple[str, ...]


@dataclass(frozen=True)
class BackupResult:
    database_path: str
    backup_path: str
    sha256: str
    size_bytes: int
    page_count: int
    migrations: tuple[str, ...]


@dataclass(frozen=True)
class RestoreResult:
    database_path: str
    restored_from: str
    safety_backup_path: str | None
    restored_sha256: str
    page_count: int
    migrations: tuple[str, ...]


def _readonly_connection(path: Path) -> sqlite3.Connection:
    uri = f"{path.resolve().as_uri()}?mode=ro"
    connection = sqlite3.connect(
        uri,
        uri=True,
        timeout=5,
        isolation_level=None,
    )
    connection.execute("PRAGMA query_only = ON")
    connection.execute("PRAGMA foreign_keys = ON")
    connection.execute("PRAGMA busy_timeout = 5000")
    return connection


def _verify_unlocked(path: Path) -> DatabaseVerification:
    if not path.is_file():
        raise SQLiteMaintenanceError(f"database file does not exist: {path}")

    try:
        connection = _readonly_connection(path)
        try:
            integrity = [
                str(row[0])
                for row in connection.execute("PRAGMA integrity_check").fetchall()
            ]
            if integrity != ["ok"]:
                raise SQLiteMaintenanceError(
                    "SQLite integrity_check failed: " + "; ".join(integrity)
                )

            foreign_key_errors = connection.execute(
                "PRAGMA foreign_key_check"
            ).fetchall()
            if foreign_key_errors:
                raise SQLiteMaintenanceError(
                    "SQLite foreign_key_check failed: "
                    f"{len(foreign_key_errors)} violation(s)"
                )

            tables = {
                str(row[0])
                for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'table'"
                ).fetchall()
            }
            missing = sorted(REQUIRED_TABLES - tables)
            if missing:
                raise SQLiteMaintenanceError(
                    "not a JARVIS Structured Memory database; missing tables: "
                    + ", ".join(missing)
                )
            migrations = tuple(
                str(row[0])
                for row in connection.execute(
                    "SELECT version FROM schema_migrations ORDER BY version"
                ).fetchall()
            )
            if "008" in migrations and "auth_sessions" not in tables:
                raise SQLiteMaintenanceError(
                    "not a JARVIS Structured Memory database; missing tables: "
                    "auth_sessions"
                )
            if "009" in migrations and "login_rate_limits" not in tables:
                raise SQLiteMaintenanceError(
                    "not a JARVIS Structured Memory database; missing tables: "
                    "login_rate_limits"
                )
            if "008" in migrations:
                _require_columns(
                    connection,
                    table="users",
                    required={
                        "username",
                        "normalized_username",
                        "display_name",
                        "password_credential",
                        "is_owner",
                    },
                )
                _require_columns(
                    connection,
                    table="auth_sessions",
                    required={
                        "id",
                        "user_id",
                        "token_hash",
                        "expires_at",
                        "revoked_at",
                    },
                )
            if "009" in migrations:
                _require_columns(
                    connection,
                    table="users",
                    required={
                        "recovery_code_hash",
                        "recovery_code_created_at",
                    },
                )
                _require_columns(
                    connection,
                    table="conversations",
                    required={
                        "title",
                        "creation_key_hash",
                        "creation_request_hash",
                    },
                )
                _require_columns(
                    connection,
                    table="login_rate_limits",
                    required={
                        "identifier_hash",
                        "failure_count",
                        "window_started_at",
                        "locked_until",
                        "updated_at",
                    },
                )

            page_count = int(
                connection.execute("PRAGMA page_count").fetchone()[0]
            )
        finally:
            connection.close()
    except SQLiteMaintenanceError:
        raise
    except sqlite3.Error as exc:
        raise SQLiteMaintenanceError(
            f"SQLite verification failed for {path}: {exc}"
        ) from exc

    return DatabaseVerification(
        database_path=str(path.resolve()),
        size_bytes=path.stat().st_size,
        page_count=page_count,
        migrations=migrations,
    )


def _require_columns(
    connection: sqlite3.Connection,
    *,
    table: str,
    required: set[str],
) -> None:
    present = {
        str(row[1])
        for row in connection.execute(f"PRAGMA table_info({table})").fetchall()
    }
    missing = sorted(required - present)
    if missing:
        raise SQLiteMaintenanceError(
            f"not a JARVIS Structured Memory database; {table} is missing "
            "columns: " + ", ".join(missing)
        )


def verify_database(database_path: Path) -> DatabaseVerification:
    database_path = Path(database_path).expanduser().resolve()
    with database_file_lock(
        database_path,
        exclusive=False,
        blocking=True,
    ):
        return _verify_unlocked(database_path)


def _temporary_database_path(parent: Path, name: str) -> Path:
    descriptor, raw_path = tempfile.mkstemp(
        prefix=f".{name}.",
        suffix=".tmp",
        dir=parent,
    )
    os.close(descriptor)
    path = Path(raw_path)
    os.chmod(path, 0o600)
    return path


def _copy_database(source_path: Path, destination_path: Path) -> None:
    source = _readonly_connection(source_path)
    destination = sqlite3.connect(destination_path, timeout=5)
    try:
        destination.execute("PRAGMA synchronous = FULL")
        source.backup(destination, pages=256, sleep=0.05)
        destination.commit()
        destination.execute("PRAGMA journal_mode = DELETE")
    except sqlite3.Error as exc:
        raise SQLiteMaintenanceError(
            f"SQLite online backup failed for {source_path}: {exc}"
        ) from exc
    finally:
        destination.close()
        source.close()


def _invalidate_restored_sessions(path: Path) -> None:
    """A restore must not resurrect copied sessions or consumed recovery codes."""

    connection = sqlite3.connect(path, timeout=5, isolation_level=None)
    try:
        has_sessions = connection.execute(
            """
            SELECT 1 FROM sqlite_master
            WHERE type = 'table' AND name = 'auth_sessions'
            """
        ).fetchone()
        user_columns = {
            str(row[1])
            for row in connection.execute("PRAGMA table_info(users)").fetchall()
        }
        has_recovery = "recovery_code_hash" in user_columns
        has_rate_limits = connection.execute(
            """
            SELECT 1 FROM sqlite_master
            WHERE type = 'table' AND name = 'login_rate_limits'
            """
        ).fetchone()
        if has_sessions is None and not has_recovery and has_rate_limits is None:
            return
        revoked_at = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        connection.execute("BEGIN IMMEDIATE")
        if has_sessions is not None:
            connection.execute(
                "UPDATE auth_sessions SET revoked_at = ? WHERE revoked_at IS NULL",
                (revoked_at,),
            )
        if has_recovery:
            connection.execute(
                """
                UPDATE users
                SET recovery_code_hash = NULL,
                    recovery_code_created_at = NULL
                WHERE recovery_code_hash IS NOT NULL
                """
            )
        if has_rate_limits is not None:
            connection.execute("DELETE FROM login_rate_limits")
        connection.commit()
    except sqlite3.Error as exc:
        connection.rollback()
        raise SQLiteMaintenanceError(
            f"could not invalidate restored authentication sessions: {exc}"
        ) from exc
    finally:
        connection.close()


def _fsync_file(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _publish_staged_database(
    staged_path: Path,
    output_path: Path,
    *,
    overwrite: bool,
) -> None:
    if overwrite:
        os.replace(staged_path, output_path)
    else:
        try:
            # Hard-link publication is atomic and refuses to overwrite a file
            # created after the earlier existence check.
            os.link(staged_path, output_path)
        except FileExistsError as exc:
            raise SQLiteMaintenanceError(
                f"output already exists: {output_path}"
            ) from exc
        else:
            staged_path.unlink()
    _fsync_directory(output_path.parent)


def _create_backup_unlocked(
    database_path: Path,
    output_path: Path,
    *,
    overwrite: bool,
) -> BackupResult:
    if database_path == output_path:
        raise SQLiteMaintenanceError("backup output must differ from the database")
    if not database_path.is_file():
        raise SQLiteMaintenanceError(
            f"database file does not exist: {database_path}"
        )
    if output_path.exists() and not overwrite:
        raise SQLiteMaintenanceError(f"output already exists: {output_path}")

    output_path.parent.mkdir(parents=True, exist_ok=True)
    staged_path = _temporary_database_path(output_path.parent, output_path.name)
    try:
        _copy_database(database_path, staged_path)
        verification = _verify_unlocked(staged_path)
        os.chmod(staged_path, 0o600)
        _fsync_file(staged_path)
        digest = _sha256_file(staged_path)
        _publish_staged_database(
            staged_path,
            output_path,
            overwrite=overwrite,
        )
    finally:
        staged_path.unlink(missing_ok=True)

    return BackupResult(
        database_path=str(database_path),
        backup_path=str(output_path),
        sha256=digest,
        size_bytes=verification.size_bytes,
        page_count=verification.page_count,
        migrations=verification.migrations,
    )


def create_backup(
    database_path: Path,
    output_path: Path,
    *,
    overwrite: bool = False,
) -> BackupResult:
    database_path = Path(database_path).expanduser().resolve()
    output_path = Path(output_path).expanduser().resolve()
    with database_file_lock(
        database_path,
        exclusive=False,
        blocking=True,
    ):
        return _create_backup_unlocked(
            database_path,
            output_path,
            overwrite=overwrite,
        )


def _default_safety_backup_path(database_path: Path) -> Path:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    candidate = database_path.with_name(
        f"{database_path.stem}.pre-restore-{stamp}{database_path.suffix}"
    )
    counter = 1
    while candidate.exists():
        candidate = database_path.with_name(
            f"{database_path.stem}.pre-restore-{stamp}-{counter}"
            f"{database_path.suffix}"
        )
        counter += 1
    return candidate


def _checkpoint_and_remove_sidecars(database_path: Path) -> None:
    try:
        connection = sqlite3.connect(database_path, timeout=1, isolation_level=None)
        try:
            connection.execute("PRAGMA busy_timeout = 1000")
            busy, _, _ = connection.execute(
                "PRAGMA wal_checkpoint(TRUNCATE)"
            ).fetchone()
            if busy:
                raise SQLiteMaintenanceError(
                    "database has an active SQLite connection; stop all clients "
                    "before restore"
                )
        finally:
            connection.close()
    except SQLiteMaintenanceError:
        raise
    except sqlite3.Error as exc:
        raise SQLiteMaintenanceError(
            f"could not checkpoint database before restore: {exc}"
        ) from exc

    wal_path = Path(f"{database_path}-wal")
    journal_path = Path(f"{database_path}-journal")
    if wal_path.exists() and wal_path.stat().st_size:
        raise SQLiteMaintenanceError(
            "non-empty WAL remains after checkpoint; restore was not started"
        )
    if journal_path.exists() and journal_path.stat().st_size:
        raise SQLiteMaintenanceError(
            "non-empty rollback journal remains; restore was not started"
        )
    for sidecar in (wal_path, Path(f"{database_path}-shm"), journal_path):
        sidecar.unlink(missing_ok=True)


def restore_database(
    backup_path: Path,
    database_path: Path,
    *,
    safety_backup_path: Path | None = None,
) -> RestoreResult:
    backup_path = Path(backup_path).expanduser().resolve()
    database_path = Path(database_path).expanduser().resolve()
    if backup_path == database_path:
        raise SQLiteMaintenanceError("backup and destination must differ")
    if not backup_path.is_file():
        raise SQLiteMaintenanceError(f"backup file does not exist: {backup_path}")

    try:
        lock = database_file_lock(
            database_path,
            exclusive=True,
            blocking=False,
        )
        with lock:
            incoming = _verify_unlocked(backup_path)
            database_path.parent.mkdir(parents=True, exist_ok=True)
            staged_path = _temporary_database_path(
                database_path.parent,
                database_path.name,
            )
            replaced = False
            safety_result: BackupResult | None = None
            try:
                _copy_database(backup_path, staged_path)
                _invalidate_restored_sessions(staged_path)
                staged_verification = _verify_unlocked(staged_path)
                os.chmod(staged_path, 0o600)
                _fsync_file(staged_path)

                if database_path.exists():
                    _checkpoint_and_remove_sidecars(database_path)
                    resolved_safety_path = (
                        Path(safety_backup_path).expanduser().resolve()
                        if safety_backup_path is not None
                        else _default_safety_backup_path(database_path)
                    )
                    if resolved_safety_path in {database_path, backup_path}:
                        raise SQLiteMaintenanceError(
                            "safety backup path must differ from source and destination"
                        )
                    safety_result = _create_backup_unlocked(
                        database_path,
                        resolved_safety_path,
                        overwrite=False,
                    )

                os.replace(staged_path, database_path)
                replaced = True
                _fsync_directory(database_path.parent)
                installed = _verify_unlocked(database_path)
                installed_digest = _sha256_file(database_path)
                if installed.page_count != staged_verification.page_count:
                    raise SQLiteMaintenanceError(
                        "restored database page count changed after atomic install"
                    )
                if installed.migrations != incoming.migrations:
                    raise SQLiteMaintenanceError(
                        "restored migration set differs from verified backup"
                    )
            except Exception as exc:
                staged_path.unlink(missing_ok=True)
                if replaced and safety_result is not None:
                    rollback_stage = _temporary_database_path(
                        database_path.parent,
                        database_path.name,
                    )
                    try:
                        _copy_database(
                            Path(safety_result.backup_path),
                            rollback_stage,
                        )
                        _verify_unlocked(rollback_stage)
                        _fsync_file(rollback_stage)
                        os.replace(rollback_stage, database_path)
                        _fsync_directory(database_path.parent)
                    finally:
                        rollback_stage.unlink(missing_ok=True)
                if isinstance(exc, SQLiteMaintenanceError):
                    raise
                raise SQLiteMaintenanceError(f"restore failed: {exc}") from exc
            finally:
                staged_path.unlink(missing_ok=True)

            return RestoreResult(
                database_path=str(database_path),
                restored_from=str(backup_path),
                safety_backup_path=(
                    safety_result.backup_path if safety_result is not None else None
                ),
                restored_sha256=installed_digest,
                page_count=installed.page_count,
                migrations=installed.migrations,
            )
    except DatabaseLockUnavailable as exc:
        raise SQLiteMaintenanceError(
            "restore refused because the JARVIS server or a backup is using the "
            "database; stop the server and retry"
        ) from exc


def _json_print(value: object) -> None:
    print(json.dumps(asdict(value), ensure_ascii=False, indent=2))


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="jarvis-db",
        description="Safe SQLite backup, verification, and offline restore",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    verify = subparsers.add_parser("verify", help="run integrity and FK checks")
    verify.add_argument("--database", type=Path, required=True)

    backup = subparsers.add_parser("backup", help="create an online backup")
    backup.add_argument("--database", type=Path, required=True)
    backup.add_argument("--output", type=Path, required=True)
    backup.add_argument("--overwrite", action="store_true")

    restore = subparsers.add_parser(
        "restore",
        help="atomically restore while the server is stopped",
    )
    restore.add_argument("--database", type=Path, required=True)
    restore.add_argument("--backup", type=Path, required=True)
    restore.add_argument("--safety-backup", type=Path)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.command == "verify":
            _json_print(verify_database(args.database))
        elif args.command == "backup":
            _json_print(
                create_backup(
                    args.database,
                    args.output,
                    overwrite=args.overwrite,
                )
            )
        else:
            _json_print(
                restore_database(
                    args.backup,
                    args.database,
                    safety_backup_path=args.safety_backup,
                )
            )
        return 0
    except SQLiteMaintenanceError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
