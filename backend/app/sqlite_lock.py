from __future__ import annotations

import fcntl
import os
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator


class DatabaseLockUnavailable(RuntimeError):
    """Raised when an exclusive maintenance lock cannot be acquired."""


def lock_path_for(database_path: Path) -> Path:
    return database_path.with_name(f"{database_path.name}.lock")


@contextmanager
def database_file_lock(
    database_path: Path,
    *,
    exclusive: bool,
    blocking: bool,
) -> Iterator[Path]:
    """Coordinate the app process and filesystem-level DB maintenance.

    Runtime processes and online backups take a shared lock. Restore takes a
    non-blocking exclusive lock, so it cannot replace the database underneath
    a running server or an in-progress backup.

    The lock file is intentionally persistent. Removing a lock file can create
    two different inodes and silently split future lock holders.
    """

    database_path = Path(database_path)
    database_path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = lock_path_for(database_path)
    descriptor = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        os.fchmod(descriptor, 0o600)
        operation = fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH
        if not blocking:
            operation |= fcntl.LOCK_NB
        try:
            fcntl.flock(descriptor, operation)
        except BlockingIOError as exc:
            raise DatabaseLockUnavailable(
                f"database is in use: {database_path}"
            ) from exc
        yield lock_path
    finally:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
        finally:
            os.close(descriptor)
