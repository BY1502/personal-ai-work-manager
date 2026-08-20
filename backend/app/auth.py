from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import json
import os
import re
import secrets
import unicodedata
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from math import ceil
from typing import Any

from fastapi import Request
from pydantic import BaseModel, ConfigDict, Field, field_validator
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import JSONResponse

from app.database import Database
from app.repository import ResourceNotFound
from app.utils import canonical_json, new_id, sha256_text


SESSION_COOKIE_NAME = "by_session"
_PASSWORD_N = 2**14
_PASSWORD_R = 8
_PASSWORD_P = 1
_PASSWORD_DKLEN = 32
_LOGIN_FAILURE_LIMIT = 5
_LOGIN_WINDOW = timedelta(minutes=15)
_LOGIN_LOCK_DURATION = timedelta(minutes=15)
_MUTATING_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})


def _validate_new_password_value(value: str) -> str:
    if not any(not character.isspace() for character in value):
        raise ValueError("password must not contain only whitespace")
    return value


class RegisterRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    username: str = Field(min_length=3, max_length=64)
    password: str = Field(min_length=10, max_length=1024)
    display_name: str | None = Field(default=None, min_length=1, max_length=80)

    @field_validator("password")
    @classmethod
    def validate_password(cls, value: str) -> str:
        return _validate_new_password_value(value)

    @field_validator("username")
    @classmethod
    def validate_username(cls, value: str) -> str:
        normalized = unicodedata.normalize("NFKC", value).strip()
        if len(normalized) < 3:
            raise ValueError("username must contain at least 3 characters")
        if not all(
            character.isalnum() or character in "._-"
            for character in normalized
        ):
            raise ValueError("username may contain letters, numbers, '.', '_' and '-'")
        return normalized

    @field_validator("display_name")
    @classmethod
    def strip_display_name(cls, value: str | None) -> str | None:
        if value is None:
            return None
        normalized = unicodedata.normalize("NFKC", value).strip()
        if not normalized:
            raise ValueError("display_name must not be blank")
        return normalized


class LoginRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    username: str = Field(min_length=1, max_length=64)
    password: str = Field(min_length=1, max_length=1024)


class ChangePasswordRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    current_password: str = Field(min_length=1, max_length=1024)
    new_password: str = Field(min_length=10, max_length=1024)

    @field_validator("new_password")
    @classmethod
    def validate_new_password(cls, value: str) -> str:
        return _validate_new_password_value(value)


class ResetPasswordRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    username: str = Field(min_length=1, max_length=64)
    recovery_code: str = Field(min_length=10, max_length=200)
    new_password: str = Field(min_length=10, max_length=1024)

    @field_validator("new_password")
    @classmethod
    def validate_new_password(cls, value: str) -> str:
        return _validate_new_password_value(value)


class RotateRecoveryCodeRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    current_password: str = Field(min_length=1, max_length=1024)


class CreateConversationRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    title: str | None = Field(default=None, min_length=1, max_length=120)

    @field_validator("title")
    @classmethod
    def strip_title(cls, value: str | None) -> str | None:
        if value is None:
            return None
        normalized = unicodedata.normalize("NFKC", value).strip()
        if not normalized:
            raise ValueError("title must not be blank")
        return normalized


@dataclass(frozen=True)
class AuthenticatedUser:
    id: str
    username: str
    display_name: str
    timezone: str
    locale: str
    is_owner: bool

    def public_dict(self) -> dict[str, str | bool]:
        return {
            "id": self.id,
            "username": self.username,
            "display_name": self.display_name,
            "timezone": self.timezone,
            "locale": self.locale,
            "is_owner": self.is_owner,
        }


@dataclass(frozen=True)
class IssuedSession:
    user: AuthenticatedUser
    token: str
    max_age_seconds: int
    recovery_code: str | None = None


class UsernameAlreadyExists(ValueError):
    pass


class InvalidCredentials(ValueError):
    pass


class InvalidRecoveryCode(ValueError):
    pass


class PasswordReuseNotAllowed(ValueError):
    pass


class LoginRateLimited(RuntimeError):
    def __init__(self, retry_after_seconds: int) -> None:
        super().__init__("too many login attempts; try again later")
        self.retry_after_seconds = max(1, retry_after_seconds)


class RecoveryRateLimited(RuntimeError):
    def __init__(self, retry_after_seconds: int) -> None:
        super().__init__("too many recovery attempts; try again later")
        self.retry_after_seconds = max(1, retry_after_seconds)


class ConversationCreationConflict(RuntimeError):
    pass


class AuthService:
    """Local account and opaque session management.

    Passwords use a per-password scrypt salt. Browser session tokens are random
    bearer secrets and only their SHA-256 digests are persisted in SQLite.
    """

    def __init__(
        self,
        database: Database,
        *,
        session_ttl_seconds: int | None = None,
    ) -> None:
        self.database = database
        self.session_ttl_seconds = (
            session_ttl_seconds
            if session_ttl_seconds is not None
            else int(
                os.getenv(
                    "AUTH_SESSION_TTL_SECONDS",
                    str(30 * 24 * 60 * 60),
                )
            )
        )
        if self.session_ttl_seconds < 60:
            raise ValueError("AUTH_SESSION_TTL_SECONDS must be at least 60")
        if self.session_ttl_seconds > 366 * 24 * 60 * 60:
            raise ValueError("AUTH_SESSION_TTL_SECONDS must not exceed 366 days")
        # An unknown username still performs the same expensive KDF operation.
        self._dummy_credential = _hash_password(secrets.token_urlsafe(24))

    def register(
        self,
        request: RegisterRequest,
        *,
        user_agent: str | None,
    ) -> tuple[IssuedSession, bool]:
        normalized_username = _normalize_username(request.username)
        credential = _hash_password(request.password)
        now = _auth_time_iso(self.database.clock.now_utc())
        token = secrets.token_urlsafe(32)
        token_hash = _token_hash(token)
        expires_at = _auth_time_iso(
            self.database.clock.now_utc()
            + timedelta(seconds=self.session_ttl_seconds)
        )
        display_name = request.display_name or request.username
        recovery_code = _new_recovery_code()
        recovery_code_hash = _recovery_code_hash(recovery_code)

        with self.database.transaction() as connection:
            self._cleanup_expired_sessions(connection, now=now)
            duplicate = connection.execute(
                "SELECT 1 FROM users WHERE normalized_username = ?",
                (normalized_username,),
            ).fetchone()
            if duplicate is not None:
                raise UsernameAlreadyExists("username is already registered")

            credentialed_count = connection.execute(
                """
                SELECT COUNT(*) AS count
                FROM users
                WHERE password_credential IS NOT NULL
                """
            ).fetchone()["count"]
            legacy_row = connection.execute(
                "SELECT id FROM users WHERE id = ? AND password_credential IS NULL",
                (self.database.default_user_id,),
            ).fetchone()
            claimed_legacy_data = credentialed_count == 0 and legacy_row is not None
            if claimed_legacy_data:
                user_id = legacy_row["id"]
                updated = connection.execute(
                    """
                    UPDATE users
                    SET username = ?,
                        normalized_username = ?,
                        display_name = ?,
                        password_credential = ?,
                        recovery_code_hash = ?,
                        recovery_code_created_at = ?,
                        is_owner = 1
                    WHERE id = ? AND password_credential IS NULL
                    """,
                    (
                        request.username,
                        normalized_username,
                        display_name,
                        credential,
                        recovery_code_hash,
                        now,
                        user_id,
                    ),
                ).rowcount
                if updated != 1:
                    raise UsernameAlreadyExists(
                        "account registration raced with another request"
                    )
            else:
                user_id = new_id("usr")
                connection.execute(
                    """
                    INSERT INTO users(
                        id, timezone, locale, created_at, username,
                        normalized_username, display_name, password_credential,
                        recovery_code_hash, recovery_code_created_at, is_owner
                    )
                    VALUES (?, ?, 'ko-KR', ?, ?, ?, ?, ?, ?, ?, 0)
                    """,
                    (
                        user_id,
                        self.database.timezone_name,
                        now,
                        request.username,
                        normalized_username,
                        display_name,
                        credential,
                        recovery_code_hash,
                        now,
                    ),
                )
            connection.execute(
                """
                INSERT INTO auth_sessions(
                    id, user_id, token_hash, created_at, expires_at,
                    revoked_at, user_agent_hash
                )
                VALUES (?, ?, ?, ?, ?, NULL, ?)
                """,
                (
                    new_id("ses"),
                    user_id,
                    token_hash,
                    now,
                    expires_at,
                    _optional_hash(user_agent),
                ),
            )
            row = connection.execute(
                """
                SELECT id, username, display_name, timezone, locale, is_owner
                FROM users WHERE id = ?
                """,
                (user_id,),
            ).fetchone()
        return (
            IssuedSession(
                user=_user_from_row(row),
                token=token,
                max_age_seconds=self.session_ttl_seconds,
                recovery_code=recovery_code,
            ),
            claimed_legacy_data,
        )

    def login(
        self,
        request: LoginRequest,
        *,
        user_agent: str | None,
    ) -> IssuedSession:
        normalized_username = _normalize_username(request.username)
        identifier_hash = _login_identifier_hash(normalized_username)
        now_datetime = self.database.clock.now_utc()
        with self.database.transaction() as connection:
            retry_after = self._rate_limit_retry_after(
                connection,
                identifier_hash=identifier_hash,
                now=now_datetime,
                delete_expired=True,
            )
        if retry_after is not None:
            raise LoginRateLimited(retry_after)

        connection = self.database.connect()
        try:
            row = connection.execute(
                """
                SELECT id, username, display_name, timezone, locale, is_owner,
                       password_credential
                FROM users
                WHERE normalized_username = ?
                """,
                (normalized_username,),
            ).fetchone()
        finally:
            connection.close()

        credential = row["password_credential"] if row else self._dummy_credential
        password_matches = _verify_password(request.password, credential)
        if row is None or not password_matches:
            with self.database.transaction() as connection:
                retry_after = self._record_auth_failure(
                    connection,
                    identifier_hash=identifier_hash,
                    now=now_datetime,
                )
            if retry_after is not None:
                raise LoginRateLimited(retry_after)
            raise InvalidCredentials("invalid username or password")

        now = _auth_time_iso(now_datetime)
        expires_at = _auth_time_iso(
            now_datetime + timedelta(seconds=self.session_ttl_seconds)
        )
        token = secrets.token_urlsafe(32)
        with self.database.transaction() as connection:
            retry_after = self._rate_limit_retry_after(
                connection,
                identifier_hash=identifier_hash,
                now=now_datetime,
                delete_expired=True,
            )
            if retry_after is not None:
                raise LoginRateLimited(retry_after)
            self._cleanup_expired_sessions(connection, now=now)
            connection.execute(
                "DELETE FROM login_rate_limits WHERE identifier_hash = ?",
                (identifier_hash,),
            )
            connection.execute(
                """
                INSERT INTO auth_sessions(
                    id, user_id, token_hash, created_at, expires_at,
                    revoked_at, user_agent_hash
                )
                VALUES (?, ?, ?, ?, ?, NULL, ?)
                """,
                (
                    new_id("ses"),
                    row["id"],
                    _token_hash(token),
                    now,
                    expires_at,
                    _optional_hash(user_agent),
                ),
            )
        return IssuedSession(
            user=_user_from_row(row),
            token=token,
            max_age_seconds=self.session_ttl_seconds,
        )

    def change_password(
        self,
        *,
        user_id: str,
        request: ChangePasswordRequest,
        user_agent: str | None,
    ) -> IssuedSession:
        connection = self.database.connect()
        try:
            row = connection.execute(
                """
                SELECT id, username, display_name, timezone, locale, is_owner,
                       password_credential, normalized_username
                FROM users
                WHERE id = ? AND password_credential IS NOT NULL
                """,
                (user_id,),
            ).fetchone()
        finally:
            connection.close()
        if row is None or not _verify_password(
            request.current_password,
            row["password_credential"],
        ):
            raise InvalidCredentials("invalid current password")
        if hmac.compare_digest(request.current_password, request.new_password):
            raise PasswordReuseNotAllowed(
                "new password must differ from current password"
            )

        new_credential = _hash_password(request.new_password)
        recovery_code = _new_recovery_code()
        now_datetime = self.database.clock.now_utc()
        now = _auth_time_iso(now_datetime)
        session = self._prepare_session(row=row, now_datetime=now_datetime)
        with self.database.transaction() as connection:
            updated = connection.execute(
                """
                UPDATE users
                SET password_credential = ?,
                    recovery_code_hash = ?,
                    recovery_code_created_at = ?
                WHERE id = ? AND password_credential = ?
                """,
                (
                    new_credential,
                    _recovery_code_hash(recovery_code),
                    now,
                    user_id,
                    row["password_credential"],
                ),
            ).rowcount
            if updated != 1:
                raise InvalidCredentials("password changed concurrently")
            self._revoke_all_sessions(connection, user_id=user_id, now=now)
            connection.execute(
                "DELETE FROM login_rate_limits WHERE identifier_hash = ?",
                (_login_identifier_hash(row["normalized_username"]),),
            )
            connection.execute(
                "DELETE FROM login_rate_limits WHERE identifier_hash = ?",
                (_recovery_identifier_hash(row["normalized_username"]),),
            )
            self._insert_session(
                connection,
                session=session,
                user_agent=user_agent,
            )
        return IssuedSession(
            user=_user_from_row(row),
            token=session["token"],
            max_age_seconds=self.session_ttl_seconds,
            recovery_code=recovery_code,
        )

    def reset_password(self, request: ResetPasswordRequest) -> str:
        normalized_username = _normalize_username(request.username)
        identifier_hash = _recovery_identifier_hash(normalized_username)
        now_datetime = self.database.clock.now_utc()
        with self.database.transaction() as connection:
            retry_after = self._rate_limit_retry_after(
                connection,
                identifier_hash=identifier_hash,
                now=now_datetime,
                delete_expired=True,
            )
        if retry_after is not None:
            raise RecoveryRateLimited(retry_after)

        supplied_hash = _recovery_code_hash(request.recovery_code)
        connection = self.database.connect()
        try:
            row = connection.execute(
                """
                SELECT id, recovery_code_hash, password_credential
                FROM users
                WHERE normalized_username = ? AND password_credential IS NOT NULL
                """,
                (normalized_username,),
            ).fetchone()
        finally:
            connection.close()
        stored_hash = (
            row["recovery_code_hash"]
            if row is not None and row["recovery_code_hash"]
            else "0" * 64
        )
        if not hmac.compare_digest(supplied_hash, stored_hash):
            with self.database.transaction() as connection:
                retry_after = self._record_auth_failure(
                    connection,
                    identifier_hash=identifier_hash,
                    now=now_datetime,
                )
            if retry_after is not None:
                raise RecoveryRateLimited(retry_after)
            raise InvalidRecoveryCode("invalid username or recovery code")
        if _verify_password(request.new_password, row["password_credential"]):
            raise PasswordReuseNotAllowed(
                "new password must differ from current password"
            )

        new_credential = _hash_password(request.new_password)
        replacement_code = _new_recovery_code()
        replacement_hash = _recovery_code_hash(replacement_code)
        now = _auth_time_iso(now_datetime)
        with self.database.transaction() as connection:
            retry_after = self._rate_limit_retry_after(
                connection,
                identifier_hash=identifier_hash,
                now=now_datetime,
                delete_expired=True,
            )
            if retry_after is not None:
                raise RecoveryRateLimited(retry_after)
            updated = connection.execute(
                """
                UPDATE users
                SET password_credential = ?,
                    recovery_code_hash = ?,
                    recovery_code_created_at = ?
                WHERE id = ? AND recovery_code_hash = ?
                """,
                (
                    new_credential,
                    replacement_hash,
                    now,
                    row["id"],
                    stored_hash,
                ),
            ).rowcount
            if updated != 1:
                raise InvalidRecoveryCode("invalid username or recovery code")
            self._revoke_all_sessions(connection, user_id=row["id"], now=now)
            connection.execute(
                "DELETE FROM login_rate_limits WHERE identifier_hash = ?",
                (_login_identifier_hash(normalized_username),),
            )
            connection.execute(
                "DELETE FROM login_rate_limits WHERE identifier_hash = ?",
                (identifier_hash,),
            )
        return replacement_code

    def rotate_recovery_code(
        self,
        *,
        user_id: str,
        current_password: str,
    ) -> str:
        connection = self.database.connect()
        try:
            row = connection.execute(
                """
                SELECT password_credential, recovery_code_hash
                FROM users
                WHERE id = ? AND password_credential IS NOT NULL
                """,
                (user_id,),
            ).fetchone()
        finally:
            connection.close()
        if row is None or not _verify_password(
            current_password,
            row["password_credential"],
        ):
            raise InvalidCredentials("invalid current password")
        recovery_code = _new_recovery_code()
        now = _auth_time_iso(self.database.clock.now_utc())
        with self.database.transaction() as connection:
            updated = connection.execute(
                """
                UPDATE users
                SET recovery_code_hash = ?, recovery_code_created_at = ?
                WHERE id = ?
                  AND password_credential = ?
                  AND recovery_code_hash IS ?
                """,
                (
                    _recovery_code_hash(recovery_code),
                    now,
                    user_id,
                    row["password_credential"],
                    row["recovery_code_hash"],
                ),
            ).rowcount
            if updated != 1:
                raise InvalidCredentials("account credentials changed concurrently")
        return recovery_code

    def logout_all(self, *, user_id: str) -> None:
        now = _auth_time_iso(self.database.clock.now_utc())
        with self.database.transaction() as connection:
            self._revoke_all_sessions(connection, user_id=user_id, now=now)

    def authenticate(self, token: str | None) -> AuthenticatedUser | None:
        if not token or len(token) > 512:
            return None
        now_datetime = self.database.clock.now_utc()
        connection = self.database.connect()
        try:
            row = connection.execute(
                """
                SELECT u.id, u.username, u.display_name, u.timezone, u.locale,
                       u.is_owner, s.expires_at
                FROM auth_sessions s
                JOIN users u ON u.id = s.user_id
                WHERE s.token_hash = ?
                  AND s.revoked_at IS NULL
                  AND u.password_credential IS NOT NULL
                """,
                (_token_hash(token),),
            ).fetchone()
        finally:
            connection.close()
        if row:
            try:
                if _parse_auth_time(row["expires_at"]) <= now_datetime:
                    return None
            except (TypeError, ValueError):
                return None
        return _user_from_row(row) if row else None

    def logout(self, token: str | None) -> None:
        if not token or len(token) > 512:
            return
        now = _auth_time_iso(self.database.clock.now_utc())
        with self.database.transaction() as connection:
            connection.execute(
                """
                UPDATE auth_sessions
                SET revoked_at = ?
                WHERE token_hash = ? AND revoked_at IS NULL
                """,
                (now, _token_hash(token)),
            )

    def _prepare_session(self, *, row, now_datetime: datetime) -> dict[str, Any]:
        return {
            "id": new_id("ses"),
            "user_id": row["id"],
            "token": secrets.token_urlsafe(32),
            "created_at": _auth_time_iso(now_datetime),
            "expires_at": _auth_time_iso(
                now_datetime + timedelta(seconds=self.session_ttl_seconds)
            ),
        }

    @staticmethod
    def _insert_session(
        connection,
        *,
        session: dict[str, Any],
        user_agent: str | None,
    ) -> None:
        connection.execute(
            """
            INSERT INTO auth_sessions(
                id, user_id, token_hash, created_at, expires_at,
                revoked_at, user_agent_hash
            )
            VALUES (?, ?, ?, ?, ?, NULL, ?)
            """,
            (
                session["id"],
                session["user_id"],
                _token_hash(session["token"]),
                session["created_at"],
                session["expires_at"],
                _optional_hash(user_agent),
            ),
        )

    @staticmethod
    def _revoke_all_sessions(connection, *, user_id: str, now: str) -> None:
        connection.execute(
            """
            UPDATE auth_sessions
            SET revoked_at = ?
            WHERE user_id = ? AND revoked_at IS NULL
            """,
            (now, user_id),
        )

    @staticmethod
    def _rate_limit_retry_after(
        connection,
        *,
        identifier_hash: str,
        now: datetime,
        delete_expired: bool,
    ) -> int | None:
        row = connection.execute(
            """
            SELECT window_started_at, locked_until
            FROM login_rate_limits
            WHERE identifier_hash = ?
            """,
            (identifier_hash,),
        ).fetchone()
        if row is None:
            return None
        locked_until = (
            _parse_auth_time(row["locked_until"])
            if row["locked_until"]
            else None
        )
        if locked_until is not None and locked_until > now:
            return ceil((locked_until - now).total_seconds())
        window_started = _parse_auth_time(row["window_started_at"])
        if locked_until is not None or now - window_started >= _LOGIN_WINDOW:
            if delete_expired:
                connection.execute(
                    "DELETE FROM login_rate_limits WHERE identifier_hash = ?",
                    (identifier_hash,),
                )
            return None
        return None

    @classmethod
    def _record_auth_failure(
        cls,
        connection,
        *,
        identifier_hash: str,
        now: datetime,
    ) -> int | None:
        connection.execute(
            """
            DELETE FROM login_rate_limits
            WHERE julianday(updated_at) < julianday(?) - 1
            """,
            (_auth_time_iso(now),),
        )
        retry_after = cls._rate_limit_retry_after(
            connection,
            identifier_hash=identifier_hash,
            now=now,
            delete_expired=True,
        )
        if retry_after is not None:
            return retry_after
        row = connection.execute(
            """
            SELECT failure_count, window_started_at
            FROM login_rate_limits
            WHERE identifier_hash = ?
            """,
            (identifier_hash,),
        ).fetchone()
        if row is None:
            failure_count = 1
            window_started_at = _auth_time_iso(now)
        else:
            failure_count = int(row["failure_count"]) + 1
            window_started_at = row["window_started_at"]
        locked_until = None
        if failure_count >= _LOGIN_FAILURE_LIMIT:
            locked_until = _auth_time_iso(now + _LOGIN_LOCK_DURATION)
            retry_after = ceil(_LOGIN_LOCK_DURATION.total_seconds())
        timestamp = _auth_time_iso(now)
        connection.execute(
            """
            INSERT INTO login_rate_limits(
                identifier_hash, failure_count, window_started_at,
                locked_until, updated_at
            )
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(identifier_hash) DO UPDATE SET
                failure_count = excluded.failure_count,
                window_started_at = excluded.window_started_at,
                locked_until = excluded.locked_until,
                updated_at = excluded.updated_at
            """,
            (
                identifier_hash,
                failure_count,
                window_started_at,
                locked_until,
                timestamp,
            ),
        )
        return retry_after

    @staticmethod
    def _cleanup_expired_sessions(connection, *, now: str) -> None:
        connection.execute(
            "DELETE FROM auth_sessions WHERE julianday(expires_at) <= julianday(?)",
            (now,),
        )


class ConversationHistoryService:
    """Rebuild browser chat bubbles from durable request and run records."""

    def __init__(self, database: Database) -> None:
        self.database = database

    def create_conversation(
        self,
        *,
        user_id: str,
        request: CreateConversationRequest,
        idempotency_key: str,
    ) -> dict[str, Any]:
        key_hash = sha256_text(f"conversation-create:v1:{idempotency_key}")
        request_hash = sha256_text(
            canonical_json({"title": request.title})
        )
        now = _auth_time_iso(self.database.clock.now_utc())
        with self.database.transaction() as connection:
            existing = connection.execute(
                """
                SELECT id, title, is_default, created_at, updated_at,
                       creation_request_hash
                FROM conversations
                WHERE user_id = ? AND creation_key_hash = ?
                """,
                (user_id, key_hash),
            ).fetchone()
            if existing is not None:
                if not hmac.compare_digest(
                    existing["creation_request_hash"],
                    request_hash,
                ):
                    raise ConversationCreationConflict(
                        "idempotency key was already used with a different request"
                    )
                return {
                    "conversation": self._conversation_summary(
                        connection,
                        user_id=user_id,
                        row=existing,
                    ),
                    "created": False,
                }

            has_conversation = connection.execute(
                "SELECT 1 FROM conversations WHERE user_id = ? LIMIT 1",
                (user_id,),
            ).fetchone()
            conversation_id = new_id("conv")
            is_default = 0 if has_conversation else 1
            connection.execute(
                """
                INSERT INTO conversations(
                    id, user_id, is_default, version, created_at, updated_at,
                    title, creation_key_hash, creation_request_hash
                )
                VALUES (?, ?, ?, 1, ?, ?, ?, ?, ?)
                """,
                (
                    conversation_id,
                    user_id,
                    is_default,
                    now,
                    now,
                    request.title,
                    key_hash,
                    request_hash,
                ),
            )
            row = connection.execute(
                """
                SELECT id, title, is_default, created_at, updated_at
                FROM conversations
                WHERE id = ? AND user_id = ?
                """,
                (conversation_id, user_id),
            ).fetchone()
            return {
                "conversation": self._conversation_summary(
                    connection,
                    user_id=user_id,
                    row=row,
                ),
                "created": True,
            }

    def list_conversations(
        self,
        *,
        user_id: str,
        limit: int,
        offset: int,
    ) -> dict[str, Any]:
        connection = self.database.connect()
        try:
            rows = connection.execute(
                """
                SELECT
                    c.id,
                    c.title,
                    c.is_default,
                    c.created_at,
                    MAX(
                        c.updated_at,
                        COALESCE(MAX(m.created_at), c.updated_at),
                        COALESCE(MAX(r.completed_at), c.updated_at)
                    ) AS updated_at,
                    COUNT(DISTINCT m.id) AS request_count,
                    COUNT(DISTINCT m.id) + COALESCE(
                        SUM(CASE WHEN r.result_json IS NOT NULL THEN 1 ELSE 0 END),
                        0
                    )
                        AS message_count
                FROM conversations c
                LEFT JOIN chat_messages m
                  ON m.conversation_id = c.id
                 AND m.user_id = c.user_id
                 AND m.role = 'USER'
                LEFT JOIN orchestration_runs r
                  ON r.request_message_id = m.id AND r.user_id = m.user_id
                WHERE c.user_id = ?
                GROUP BY c.id
                ORDER BY updated_at DESC, c.id DESC
                LIMIT ? OFFSET ?
                """,
                (user_id, limit + 1, offset),
            ).fetchall()
            items = rows[:limit]
            result: list[dict[str, Any]] = []
            for row in items:
                preview = connection.execute(
                    """
                    SELECT content
                    FROM chat_messages
                    WHERE conversation_id = ? AND user_id = ? AND role = 'USER'
                    ORDER BY server_sequence DESC
                    LIMIT 1
                    """,
                    (row["id"], user_id),
                ).fetchone()
                result.append(
                    {
                        "id": row["id"],
                        "title": row["title"],
                        "is_default": bool(row["is_default"]),
                        "created_at": row["created_at"],
                        "updated_at": row["updated_at"],
                        "request_count": row["request_count"],
                        "message_count": row["message_count"],
                        "last_message_preview": (
                            preview["content"][:160] if preview else None
                        ),
                    }
                )
            return {
                "items": result,
                "limit": limit,
                "offset": offset,
                "has_more": len(rows) > limit,
            }
        finally:
            connection.close()

    @staticmethod
    def _conversation_summary(connection, *, user_id: str, row) -> dict[str, Any]:
        counts = connection.execute(
            """
            SELECT
                COUNT(DISTINCT m.id) AS request_count,
                COUNT(DISTINCT m.id) + COALESCE(
                    SUM(CASE WHEN r.result_json IS NOT NULL THEN 1 ELSE 0 END),
                    0
                ) AS message_count,
                (
                    SELECT content
                    FROM chat_messages latest
                    WHERE latest.conversation_id = ?
                      AND latest.user_id = ?
                      AND latest.role = 'USER'
                    ORDER BY latest.server_sequence DESC
                    LIMIT 1
                ) AS last_message_preview
            FROM chat_messages m
            LEFT JOIN orchestration_runs r
              ON r.request_message_id = m.id AND r.user_id = m.user_id
            WHERE m.conversation_id = ? AND m.user_id = ? AND m.role = 'USER'
            """,
            (row["id"], user_id, row["id"], user_id),
        ).fetchone()
        return {
            "id": row["id"],
            "title": row["title"],
            "is_default": bool(row["is_default"]),
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
            "request_count": counts["request_count"],
            "message_count": counts["message_count"],
            "last_message_preview": (
                counts["last_message_preview"][:160]
                if counts["last_message_preview"]
                else None
            ),
        }

    def messages(
        self,
        *,
        user_id: str,
        conversation_id: str,
        limit: int,
        before_sequence: int | None,
    ) -> dict[str, Any]:
        connection = self.database.connect()
        try:
            owned = connection.execute(
                "SELECT 1 FROM conversations WHERE id = ? AND user_id = ?",
                (conversation_id, user_id),
            ).fetchone()
            if owned is None:
                raise ResourceNotFound("conversation not found")
            sequence_filter = (
                "AND m.server_sequence < ?" if before_sequence is not None else ""
            )
            parameters: list[Any] = [conversation_id, user_id]
            if before_sequence is not None:
                parameters.append(before_sequence)
            parameters.append(limit + 1)
            rows = connection.execute(
                f"""
                SELECT
                    m.id AS message_id,
                    m.client_message_id,
                    m.server_sequence,
                    m.content,
                    m.created_at AS message_created_at,
                    r.id AS run_id,
                    r.status AS run_status,
                    r.result_json,
                    r.completed_at
                FROM chat_messages m
                LEFT JOIN orchestration_runs r
                  ON r.request_message_id = m.id AND r.user_id = m.user_id
                WHERE m.conversation_id = ? AND m.user_id = ? AND m.role = 'USER'
                  {sequence_filter}
                ORDER BY m.server_sequence DESC
                LIMIT ?
                """,
                parameters,
            ).fetchall()
            has_more = len(rows) > limit
            selected = list(reversed(rows[:limit]))
            items: list[dict[str, Any]] = []
            for row in selected:
                user_cursor = row["server_sequence"] * 2
                items.append(
                    {
                        "id": row["message_id"],
                        "role": "user",
                        "content": row["content"],
                        "sequence_cursor": user_cursor,
                        "server_sequence": row["server_sequence"],
                        "created_at": row["message_created_at"],
                        "run_id": row["run_id"],
                        "run_status": row["run_status"],
                        "client_message_id": row["client_message_id"],
                        "response": None,
                    }
                )
                if row["result_json"]:
                    response = json.loads(row["result_json"])
                    items.append(
                        {
                            "id": f"assistant-{row['run_id']}",
                            "role": "assistant",
                            "content": response.get("display_response", ""),
                            "sequence_cursor": user_cursor + 1,
                            "server_sequence": row["server_sequence"],
                            "created_at": (
                                row["completed_at"]
                                or row["message_created_at"]
                            ),
                            "run_id": row["run_id"],
                            "run_status": row["run_status"],
                            "client_message_id": row["client_message_id"],
                            "response": response,
                        }
                    )
            return {
                "conversation_id": conversation_id,
                "items": items,
                "has_more": has_more,
                "next_before_sequence": (
                    selected[0]["server_sequence"] if has_more and selected else None
                ),
            }
        finally:
            connection.close()


class LocalAuthenticationMiddleware(BaseHTTPMiddleware):
    """Require an active cookie for business APIs and reject browser CSRF origins."""

    def __init__(
        self,
        app,
        *,
        auth: AuthService,
        trusted_origins: set[str],
        trusted_origin_pattern: str | None,
        auth_required: bool,
    ) -> None:
        super().__init__(app)
        self.auth = auth
        self.trusted_origins = trusted_origins
        self.trusted_origin_regex = (
            re.compile(trusted_origin_pattern) if trusted_origin_pattern else None
        )
        self.auth_required = auth_required

    async def dispatch(self, request: Request, call_next):
        if request.method == "OPTIONS":
            return await call_next(request)

        origin = request.headers.get("origin")
        if (
            origin
            and request.url.path.startswith("/api/v1/")
            and request.method in _MUTATING_METHODS
            and not self._origin_is_trusted(origin)
        ):
            response = _auth_problem(
                403, "UNTRUSTED_ORIGIN", "request origin is not allowed"
            )
            response.headers["Cache-Control"] = "private, no-store"
            return response

        public_path = request.url.path in {
            "/api/v1/auth/register",
            "/api/v1/auth/login",
            "/api/v1/auth/logout",
            "/api/v1/auth/password/reset",
        }
        business_path = request.url.path.startswith("/api/v1/") and not public_path
        if business_path and self.auth_required:
            token = request.cookies.get(SESSION_COOKIE_NAME)
            user = self.auth.authenticate(token)
            if user is None:
                response = _auth_problem(
                    401,
                    "AUTHENTICATION_REQUIRED",
                    "login is required",
                )
                response.headers["Cache-Control"] = "private, no-store"
                return response
            request.state.auth_user = user
        elif business_path:
            # Compatibility mode exists only for legacy backend tests and
            # explicit local migration tools. Production defaults to required.
            request.state.auth_user = AuthenticatedUser(
                id=self.auth.database.default_user_id,
                username="local-user",
                display_name="local-user",
                timezone=self.auth.database.timezone_name,
                locale="ko-KR",
                is_owner=True,
            )
        response = await call_next(request)
        if request.url.path.startswith("/api/v1/"):
            response.headers["Cache-Control"] = "private, no-store"
        return response

    def _origin_is_trusted(self, origin: str) -> bool:
        return origin in self.trusted_origins or bool(
            self.trusted_origin_regex
            and self.trusted_origin_regex.fullmatch(origin)
        )


def _normalize_username(value: str) -> str:
    return unicodedata.normalize("NFKC", value).strip().casefold()


def _login_identifier_hash(normalized_username: str) -> str:
    # Only a one-way, namespaced identifier is stored in the throttle table.
    return sha256_text(f"login-rate-limit:v1:{normalized_username}")


def _recovery_identifier_hash(normalized_username: str) -> str:
    return sha256_text(f"recovery-rate-limit:v1:{normalized_username}")


def _new_recovery_code() -> str:
    encoded = base64.b32encode(secrets.token_bytes(20)).decode("ascii").rstrip("=")
    groups = [encoded[index : index + 4] for index in range(0, len(encoded), 4)]
    return "BYRC-" + "-".join(groups)


def _normalize_recovery_code(value: str) -> str:
    normalized = unicodedata.normalize("NFKC", value).upper()
    return "".join(character for character in normalized if character.isalnum())


def _recovery_code_hash(value: str) -> str:
    return sha256_text(f"recovery-code:v1:{_normalize_recovery_code(value)}")


def _auth_time_iso(value: datetime) -> str:
    return (
        value.astimezone(timezone.utc)
        .isoformat(timespec="microseconds")
        .replace("+00:00", "Z")
    )


def _parse_auth_time(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(
        timezone.utc
    )


def _hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(
        password.encode("utf-8"),
        salt=salt,
        n=_PASSWORD_N,
        r=_PASSWORD_R,
        p=_PASSWORD_P,
        dklen=_PASSWORD_DKLEN,
    )
    return "$".join(
        (
            "scrypt-v1",
            str(_PASSWORD_N),
            str(_PASSWORD_R),
            str(_PASSWORD_P),
            base64.urlsafe_b64encode(salt).decode("ascii"),
            base64.urlsafe_b64encode(digest).decode("ascii"),
        )
    )


def _verify_password(password: str, credential: str) -> bool:
    try:
        algorithm, n, r, p, encoded_salt, encoded_digest = credential.split("$", 5)
        if algorithm != "scrypt-v1":
            return False
        parsed_n, parsed_r, parsed_p = int(n), int(r), int(p)
        if (parsed_n, parsed_r, parsed_p) != (
            _PASSWORD_N,
            _PASSWORD_R,
            _PASSWORD_P,
        ):
            return False
        salt = base64.urlsafe_b64decode(encoded_salt.encode("ascii"))
        expected = base64.urlsafe_b64decode(encoded_digest.encode("ascii"))
        if len(salt) != 16 or len(expected) != _PASSWORD_DKLEN:
            return False
        actual = hashlib.scrypt(
            password.encode("utf-8"),
            salt=salt,
            n=parsed_n,
            r=parsed_r,
            p=parsed_p,
            dklen=len(expected),
        )
    except (binascii.Error, OverflowError, ValueError, TypeError):
        return False
    return hmac.compare_digest(actual, expected)


def _token_hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _optional_hash(value: str | None) -> str | None:
    return _token_hash(value) if value else None


def _user_from_row(row) -> AuthenticatedUser:
    username = row["username"]
    return AuthenticatedUser(
        id=row["id"],
        username=username,
        display_name=row["display_name"] or username,
        timezone=row["timezone"],
        locale=row["locale"],
        is_owner=bool(row["is_owner"]),
    )


def _auth_problem(status_code: int, code: str, detail: str) -> JSONResponse:
    return JSONResponse(
        status_code=status_code,
        content={"error": {"code": code, "detail": detail}},
    )
