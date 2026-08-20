from __future__ import annotations

import hashlib
import sqlite3
import stat
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.auth import (
    SESSION_COOKIE_NAME,
    AuthService,
    ChangePasswordRequest,
    ConversationHistoryService,
    CreateConversationRequest,
    InvalidCredentials,
    LoginRateLimited,
    LoginRequest,
)
from app.database import Database
from app.extraction import DeterministicTestProvider
from app.main import create_app
from app.sqlite_maintenance import (
    SQLiteMaintenanceError,
    create_backup,
    restore_database,
    verify_database,
)
from app.utils import FrozenClock


PASSWORD = "correct horse battery staple"
NEW_PASSWORD = "an even better battery staple"


def _app(tmp_path: Path, *, clock: FrozenClock | None = None):
    return create_app(
        database_path=tmp_path / "account-security.sqlite",
        clock=clock
        or FrozenClock(datetime(2026, 8, 20, 1, 0, tzinfo=timezone.utc)),
        extractor=DeterministicTestProvider(),
        auth_required=True,
    )


def _register(
    client: TestClient,
    username: str,
    *,
    password: str = PASSWORD,
) -> tuple[dict, str]:
    response = client.post(
        "/api/v1/auth/register",
        json={"username": username, "password": password},
    )
    assert response.status_code == 201, response.text
    body = response.json()
    assert isinstance(body["recovery_code"], str)
    return body["user"], body["recovery_code"]


def _login(client: TestClient, username: str, password: str = PASSWORD):
    return client.post(
        "/api/v1/auth/login",
        json={"username": username, "password": password},
    )


def test_recovery_code_is_returned_once_hashed_at_rest_and_rotated_on_use(
    tmp_path: Path,
) -> None:
    application = _app(tmp_path)
    with TestClient(application) as client:
        registration = client.post(
            "/api/v1/auth/register",
            json={"username": "owner", "password": PASSWORD},
        )
        assert registration.status_code == 201
        code = registration.json()["recovery_code"]
        assert code.startswith("BYRC-")
        assert registration.text.count(code) == 1
        assert "no-store" in registration.headers["cache-control"]

        me = client.get("/api/v1/auth/me")
        assert me.status_code == 200
        assert code not in me.text
        assert "recovery_code" not in me.json()

        connection = application.state.database.connect()
        try:
            row = connection.execute(
                """
                SELECT recovery_code_hash, recovery_code_created_at
                FROM users WHERE normalized_username = 'owner'
                """
            ).fetchone()
            database_dump = "\n".join(connection.iterdump())
        finally:
            connection.close()
        assert row["recovery_code_hash"]
        assert len(row["recovery_code_hash"]) == 64
        assert row["recovery_code_created_at"]
        assert code != row["recovery_code_hash"]
        assert code not in database_dump

        denied_rotation = client.post(
            "/api/v1/auth/recovery-code/rotate",
            json={"current_password": "wrong password"},
        )
        assert denied_rotation.status_code == 401
        rotated = client.post(
            "/api/v1/auth/recovery-code/rotate",
            json={"current_password": PASSWORD},
        )
        assert rotated.status_code == 200
        rotated_code = rotated.json()["recovery_code"]
        assert rotated_code != code
        assert "no-store" in rotated.headers["cache-control"]

        old_code = client.post(
            "/api/v1/auth/password/reset",
            json={
                "username": "owner",
                "recovery_code": code,
                "new_password": NEW_PASSWORD,
            },
        )
        assert old_code.status_code == 401
        assert old_code.json()["error"] == {
            "code": "INVALID_RECOVERY_CODE",
            "detail": "invalid username or recovery code",
        }

        reset = client.post(
            "/api/v1/auth/password/reset",
            json={
                "username": "owner",
                "recovery_code": rotated_code,
                "new_password": NEW_PASSWORD,
            },
        )
        assert reset.status_code == 200
        replacement_code = reset.json()["recovery_code"]
        assert replacement_code not in {code, rotated_code}
        assert "no-store" in reset.headers["cache-control"]
        assert client.get("/api/v1/auth/me").status_code == 401

        replay = client.post(
            "/api/v1/auth/password/reset",
            json={
                "username": "owner",
                "recovery_code": rotated_code,
                "new_password": PASSWORD,
            },
        )
        assert replay.status_code == 401
        assert _login(client, "owner", PASSWORD).status_code == 401
        assert _login(client, "owner", NEW_PASSWORD).status_code == 200


def test_public_reset_is_enumeration_resistant_and_csrf_protected(
    tmp_path: Path,
) -> None:
    application = _app(tmp_path)
    with TestClient(application) as client:
        _, code = _register(client, "owner")
        wrong_known = client.post(
            "/api/v1/auth/password/reset",
            json={
                "username": "owner",
                "recovery_code": "BYRC-THIS-CODE-IS-NOT-VALID",
                "new_password": NEW_PASSWORD,
            },
        )
        wrong_unknown = client.post(
            "/api/v1/auth/password/reset",
            json={
                "username": "missing-user",
                "recovery_code": "BYRC-THIS-CODE-IS-NOT-VALID",
                "new_password": NEW_PASSWORD,
            },
        )
        assert wrong_known.status_code == wrong_unknown.status_code == 401
        assert wrong_known.json() == wrong_unknown.json()

        csrf = client.post(
            "/api/v1/auth/password/reset",
            headers={"Origin": "https://attacker.example"},
            json={
                "username": "owner",
                "recovery_code": code,
                "new_password": NEW_PASSWORD,
            },
        )
        assert csrf.status_code == 403
        assert _login(client, "owner", PASSWORD).status_code == 200


def test_concurrent_recovery_replay_has_exactly_one_winner(tmp_path: Path) -> None:
    application = _app(tmp_path)
    with TestClient(application) as bootstrap:
        _, code = _register(bootstrap, "owner")

    barrier = threading.Barrier(2)
    results: list[tuple[str, int]] = []
    result_lock = threading.Lock()
    candidates = {
        "first": "first replacement password",
        "second": "second replacement password",
    }

    def reset(label: str, password: str) -> None:
        with TestClient(application) as client:
            barrier.wait(timeout=3)
            response = client.post(
                "/api/v1/auth/password/reset",
                json={
                    "username": "owner",
                    "recovery_code": code,
                    "new_password": password,
                },
            )
            with result_lock:
                results.append((label, response.status_code))

    threads = [
        threading.Thread(target=reset, args=item)
        for item in candidates.items()
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=15)

    assert sorted(status for _, status in results) == [200, 401]
    winning_label = next(label for label, status in results if status == 200)
    losing_label = next(label for label, status in results if status == 401)
    with TestClient(application) as client:
        assert _login(client, "owner", candidates[winning_label]).status_code == 200
        assert _login(client, "owner", candidates[losing_label]).status_code == 401


def test_password_change_and_logout_all_revoke_only_the_target_users_sessions(
    tmp_path: Path,
) -> None:
    application = _app(tmp_path)
    with (
        TestClient(application) as primary,
        TestClient(application) as secondary,
        TestClient(application) as member,
    ):
        owner, old_recovery_code = _register(primary, "owner")
        assert _login(secondary, "owner").status_code == 200
        old_primary_token = primary.cookies.get(SESSION_COOKIE_NAME)
        old_secondary_token = secondary.cookies.get(SESSION_COOKIE_NAME)
        member_user, _ = _register(member, "member")

        changed = primary.post(
            "/api/v1/auth/password/change",
            json={
                "current_password": PASSWORD,
                "new_password": NEW_PASSWORD,
            },
        )
        assert changed.status_code == 200
        assert changed.json()["user"] == owner
        changed_recovery_code = changed.json()["recovery_code"]
        assert changed_recovery_code.startswith("BYRC-")
        assert changed_recovery_code != old_recovery_code
        new_primary_token = primary.cookies.get(SESSION_COOKIE_NAME)
        assert new_primary_token not in {None, old_primary_token, old_secondary_token}
        assert primary.get("/api/v1/auth/me").status_code == 200
        assert secondary.get("/api/v1/auth/me").status_code == 401
        assert member.get("/api/v1/auth/me").json()["user"] == member_user

        with TestClient(application) as old_session:
            old_session.cookies.set(
                SESSION_COOKIE_NAME,
                old_primary_token,
                domain="testserver.local",
                path="/",
            )
            assert old_session.get("/api/v1/auth/me").status_code == 401

        assert _login(secondary, "owner", PASSWORD).status_code == 401
        assert _login(secondary, "owner", NEW_PASSWORD).status_code == 200
        stale_recovery = secondary.post(
            "/api/v1/auth/password/reset",
            json={
                "username": "owner",
                "recovery_code": old_recovery_code,
                "new_password": "a third replacement password",
            },
        )
        assert stale_recovery.status_code == 401
        assert primary.post("/api/v1/auth/logout-all").status_code == 204
        assert primary.get("/api/v1/auth/me").status_code == 401
        assert secondary.get("/api/v1/auth/me").status_code == 401
        assert member.get("/api/v1/auth/me").status_code == 200


def test_wrong_current_password_and_password_reuse_are_non_mutating(
    tmp_path: Path,
) -> None:
    application = _app(tmp_path)
    with TestClient(application) as client:
        _register(client, "owner")
        original_cookie = client.cookies.get(SESSION_COOKIE_NAME)
        wrong = client.post(
            "/api/v1/auth/password/change",
            json={
                "current_password": "not the current password",
                "new_password": NEW_PASSWORD,
            },
        )
        assert wrong.status_code == 401
        assert client.cookies.get(SESSION_COOKIE_NAME) == original_cookie
        assert client.get("/api/v1/auth/me").status_code == 200

        reused = client.post(
            "/api/v1/auth/password/change",
            json={"current_password": PASSWORD, "new_password": PASSWORD},
        )
        assert reused.status_code == 422
        assert reused.json()["error"]["code"] == "PASSWORD_REUSE_NOT_ALLOWED"
        assert client.cookies.get(SESSION_COOKIE_NAME) == original_cookie
        assert _login(client, "owner", PASSWORD).status_code == 200


def test_concurrent_password_change_and_recovery_rotation_have_one_winner(
    tmp_path: Path,
) -> None:
    application = _app(tmp_path)
    with TestClient(application) as client:
        user, _ = _register(client, "owner")
    auth = AuthService(application.state.database)

    password_barrier = threading.Barrier(2)
    password_results: list[tuple[str, object]] = []
    result_lock = threading.Lock()
    candidates = {
        "first": "first concurrent replacement",
        "second": "second concurrent replacement",
    }

    def change(label: str, password: str) -> None:
        password_barrier.wait(timeout=3)
        try:
            result: object = auth.change_password(
                user_id=user["id"],
                request=ChangePasswordRequest(
                    current_password=PASSWORD,
                    new_password=password,
                ),
                user_agent="credential-race-regression",
            )
        except InvalidCredentials as error:
            result = error
        with result_lock:
            password_results.append((label, result))

    threads = [
        threading.Thread(target=change, args=item)
        for item in candidates.items()
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=20)

    winners = [
        (label, result)
        for label, result in password_results
        if not isinstance(result, InvalidCredentials)
    ]
    assert len(winners) == 1
    assert sum(
        isinstance(result, InvalidCredentials)
        for _, result in password_results
    ) == 1
    winning_label, winning_session = winners[0]
    winning_password = candidates[winning_label]
    assert winning_session.recovery_code.startswith("BYRC-")
    assert auth.authenticate(winning_session.token) is not None

    rotation_barrier = threading.Barrier(2)
    rotation_results: list[object] = []

    def rotate() -> None:
        rotation_barrier.wait(timeout=3)
        try:
            result: object = auth.rotate_recovery_code(
                user_id=user["id"],
                current_password=winning_password,
            )
        except InvalidCredentials as error:
            result = error
        with result_lock:
            rotation_results.append(result)

    threads = [threading.Thread(target=rotate) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=20)

    assert sum(isinstance(result, str) for result in rotation_results) == 1
    assert sum(
        isinstance(result, InvalidCredentials) for result in rotation_results
    ) == 1


def test_login_rate_limit_is_generic_persistent_and_cleared_on_success(
    tmp_path: Path,
) -> None:
    clock = FrozenClock(datetime(2026, 8, 20, 1, 0, tzinfo=timezone.utc))
    application = _app(tmp_path, clock=clock)
    with TestClient(application) as client:
        _register(client, "owner")
        known = _login(client, "owner", "wrong password")
        unknown = _login(client, "missing-user", "wrong password")
        assert known.status_code == unknown.status_code == 401
        assert known.json() == unknown.json()

        for _ in range(3):
            assert _login(client, "owner", "wrong password").status_code == 401
        fifth = _login(client, "owner", "wrong password")
        assert fifth.status_code == 429
        assert fifth.json()["error"]["code"] == "TOO_MANY_LOGIN_ATTEMPTS"
        assert fifth.headers["retry-after"] == "900"
        assert _login(client, "owner", PASSWORD).status_code == 429

    # The lock is a SQLite record, not process memory.
    restarted = _app(tmp_path, clock=clock)
    with TestClient(restarted) as client:
        assert _login(client, "owner", PASSWORD).status_code == 429
        clock.set(clock.now_utc() + timedelta(minutes=15))
        assert _login(client, "owner", PASSWORD).status_code == 200
        assert _login(client, "owner", "wrong password").status_code == 401

    connection = restarted.state.database.connect()
    try:
        rows = connection.execute(
            "SELECT identifier_hash, failure_count FROM login_rate_limits"
        ).fetchall()
    finally:
        connection.close()
    assert rows
    assert all(len(row["identifier_hash"]) == 64 for row in rows)
    assert all("owner" not in row["identifier_hash"] for row in rows)
    assert all(row["identifier_hash"] == row["identifier_hash"].lower() for row in rows)


def test_recovery_rate_limit_is_generic_persistent_and_success_clears_it(
    tmp_path: Path,
) -> None:
    clock = FrozenClock(datetime(2026, 8, 20, 1, 0, tzinfo=timezone.utc))
    application = _app(tmp_path, clock=clock)
    with TestClient(application) as client:
        _, code = _register(client, "owner")
        wrong_payload = {
            "recovery_code": "BYRC-THIS-CODE-IS-NOT-VALID",
            "new_password": NEW_PASSWORD,
        }
        known = client.post(
            "/api/v1/auth/password/reset",
            json={"username": "owner", **wrong_payload},
        )
        unknown = client.post(
            "/api/v1/auth/password/reset",
            json={"username": "missing-user", **wrong_payload},
        )
        assert known.status_code == unknown.status_code == 401
        assert known.json() == unknown.json()

        for _ in range(3):
            assert client.post(
                "/api/v1/auth/password/reset",
                json={"username": "owner", **wrong_payload},
            ).status_code == 401
        fifth = client.post(
            "/api/v1/auth/password/reset",
            json={"username": "owner", **wrong_payload},
        )
        assert fifth.status_code == 429
        assert fifth.json()["error"]["code"] == "TOO_MANY_RECOVERY_ATTEMPTS"
        assert fifth.headers["retry-after"] == "900"
        assert client.post(
            "/api/v1/auth/password/reset",
            json={
                "username": "owner",
                "recovery_code": code,
                "new_password": NEW_PASSWORD,
            },
        ).status_code == 429

    restarted = _app(tmp_path, clock=clock)
    with TestClient(restarted) as client:
        assert client.post(
            "/api/v1/auth/password/reset",
            json={
                "username": "owner",
                "recovery_code": code,
                "new_password": NEW_PASSWORD,
            },
        ).status_code == 429
        clock.set(clock.now_utc() + timedelta(minutes=15))
        successful = client.post(
            "/api/v1/auth/password/reset",
            json={
                "username": "owner",
                "recovery_code": code,
                "new_password": NEW_PASSWORD,
            },
        )
        assert successful.status_code == 200

    connection = restarted.state.database.connect()
    try:
        owner_hash = hashlib.sha256(
            b"recovery-rate-limit:v1:owner"
        ).hexdigest()
        assert connection.execute(
            """
            SELECT COUNT(*) FROM login_rate_limits
            WHERE identifier_hash = ?
            """,
            (owner_hash,),
        ).fetchone()[0] == 0
    finally:
        connection.close()


def test_concurrent_failed_logins_are_serialized_at_the_limit(tmp_path: Path) -> None:
    application = _app(tmp_path)
    with TestClient(application) as client:
        _register(client, "race-user")
    auth = AuthService(application.state.database)
    barrier = threading.Barrier(6)
    outcomes: list[str] = []
    lock = threading.Lock()

    def fail_login() -> None:
        barrier.wait(timeout=5)
        try:
            auth.login(
                LoginRequest(username="race-user", password="wrong password"),
                user_agent="rate-limit-regression",
            )
        except InvalidCredentials:
            outcome = "invalid"
        except LoginRateLimited:
            outcome = "limited"
        else:  # pragma: no cover - a wrong password must never authenticate
            outcome = "authenticated"
        with lock:
            outcomes.append(outcome)

    threads = [threading.Thread(target=fail_login) for _ in range(6)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=20)

    assert sorted(outcomes) == ["invalid"] * 4 + ["limited"] * 2
    connection = application.state.database.connect()
    try:
        row = connection.execute(
            "SELECT failure_count, locked_until FROM login_rate_limits"
        ).fetchone()
    finally:
        connection.close()
    assert row["failure_count"] == 5
    assert row["locked_until"] is not None


def test_conversation_creation_is_idempotent_user_scoped_and_idor_safe(
    tmp_path: Path,
) -> None:
    application = _app(tmp_path)
    idempotency_key = "conversation-request-0001"
    with TestClient(application) as owner, TestClient(application) as member:
        owner_user, _ = _register(owner, "owner")
        first = owner.post(
            "/api/v1/chat/conversations",
            headers={"Idempotency-Key": idempotency_key},
            json={"title": "  업무 정리  "},
        )
        assert first.status_code == 201
        assert first.json()["created"] is True
        assert first.json()["conversation"]["title"] == "업무 정리"
        assert "no-store" in first.headers["cache-control"]
        conversation_id = first.json()["conversation"]["id"]

        replay = owner.post(
            "/api/v1/chat/conversations",
            headers={"Idempotency-Key": idempotency_key},
            json={"title": "업무 정리"},
        )
        assert replay.status_code == 200
        assert replay.json()["created"] is False
        assert replay.json()["conversation"]["id"] == conversation_id

        conflict = owner.post(
            "/api/v1/chat/conversations",
            headers={"Idempotency-Key": idempotency_key},
            json={"title": "다른 제목"},
        )
        assert conflict.status_code == 409
        assert conflict.json()["error"]["code"] == "IDEMPOTENCY_KEY_CONFLICT"

        member_user, _ = _register(member, "member")
        member_created = member.post(
            "/api/v1/chat/conversations",
            headers={"Idempotency-Key": idempotency_key},
            json={"title": "회원 대화"},
        )
        assert member_created.status_code == 201
        assert member_created.json()["conversation"]["id"] != conversation_id
        assert member.get(
            f"/api/v1/chat/conversations/{conversation_id}/messages"
        ).status_code == 404
        cross_write = member.post(
            "/api/v1/chat/runs",
            json={
                "conversation_id": conversation_id,
                "client_message_id": "cross-account-message",
                "content": "다른 사용자의 대화에 쓰기",
            },
        )
        assert cross_write.status_code == 404

        owner_items = owner.get("/api/v1/chat/conversations").json()["items"]
        member_items = member.get("/api/v1/chat/conversations").json()["items"]
        assert {item["title"] for item in owner_items} == {"업무 정리"}
        assert {item["title"] for item in member_items} == {"회원 대화"}

    connection = application.state.database.connect()
    try:
        rows = connection.execute(
            """
            SELECT user_id, creation_key_hash, creation_request_hash
            FROM conversations WHERE creation_key_hash IS NOT NULL
            """
        ).fetchall()
    finally:
        connection.close()
    assert {row["user_id"] for row in rows} == {owner_user["id"], member_user["id"]}
    assert all(row["creation_key_hash"] != idempotency_key for row in rows)
    assert all(len(row["creation_key_hash"]) == 64 for row in rows)
    assert all(len(row["creation_request_hash"]) == 64 for row in rows)


def test_concurrent_conversation_create_replays_one_durable_row(tmp_path: Path) -> None:
    application = _app(tmp_path)
    with TestClient(application) as client:
        user, _ = _register(client, "owner")
    history = ConversationHistoryService(application.state.database)
    barrier = threading.Barrier(2)
    created: list[bool] = []
    errors: list[BaseException] = []

    def create() -> None:
        try:
            barrier.wait(timeout=3)
            result = history.create_conversation(
                user_id=user["id"],
                request=CreateConversationRequest(title="동시 요청"),
                idempotency_key="same-concurrent-request",
            )
            created.append(result["created"])
        except BaseException as error:  # asserted on the main thread
            errors.append(error)

    threads = [threading.Thread(target=create) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)

    assert not errors
    assert sorted(created) == [False, True]
    connection = application.state.database.connect()
    try:
        count = connection.execute(
            "SELECT COUNT(*) FROM conversations WHERE user_id = ?",
            (user["id"],),
        ).fetchone()[0]
    finally:
        connection.close()
    assert count == 1


def test_restore_preserves_accounts_and_conversations_but_invalidates_secrets(
    tmp_path: Path,
) -> None:
    application = _app(tmp_path)
    with TestClient(application) as client:
        _, recovery_code = _register(client, "owner")
        token = client.cookies.get(SESSION_COOKIE_NAME)
        created = client.post(
            "/api/v1/chat/conversations",
            headers={"Idempotency-Key": "backup-conversation-key"},
            json={"title": "백업할 대화"},
        )
        assert created.status_code == 201
        assert _login(client, "owner", "wrong password").status_code == 401

    source = application.state.database.connect()
    try:
        credential = source.execute(
            "SELECT password_credential FROM users WHERE normalized_username = 'owner'"
        ).fetchone()[0]
    finally:
        source.close()

    backup_path = tmp_path / "account-backup.sqlite"
    restored_path = tmp_path / "account-restored.sqlite"
    create_backup(application.state.database.path, backup_path)
    restore_database(backup_path, restored_path)
    restored = create_app(
        database_path=restored_path,
        clock=FrozenClock(datetime(2026, 8, 20, 1, 0, tzinfo=timezone.utc)),
        extractor=DeterministicTestProvider(),
        auth_required=True,
    )
    connection = restored.state.database.connect()
    try:
        user_row = connection.execute(
            """
            SELECT password_credential, recovery_code_hash,
                   recovery_code_created_at
            FROM users WHERE normalized_username = 'owner'
            """
        ).fetchone()
        sessions = connection.execute(
            "SELECT revoked_at FROM auth_sessions"
        ).fetchall()
        limiter_count = connection.execute(
            "SELECT COUNT(*) FROM login_rate_limits"
        ).fetchone()[0]
        conversation = connection.execute(
            "SELECT title FROM conversations WHERE title = '백업할 대화'"
        ).fetchone()
    finally:
        connection.close()

    assert user_row["password_credential"] == credential
    assert user_row["recovery_code_hash"] is None
    assert user_row["recovery_code_created_at"] is None
    assert sessions and all(row["revoked_at"] for row in sessions)
    assert limiter_count == 0
    assert conversation is not None
    assert AuthService(restored.state.database).authenticate(token) is None

    with TestClient(restored) as client:
        unusable_backup_code = client.post(
            "/api/v1/auth/password/reset",
            json={
                "username": "owner",
                "recovery_code": recovery_code,
                "new_password": NEW_PASSWORD,
            },
        )
        assert unusable_backup_code.status_code == 401
        assert _login(client, "owner", PASSWORD).status_code == 200
        assert client.post(
            "/api/v1/auth/recovery-code/rotate",
            json={"current_password": PASSWORD},
        ).status_code == 200


def test_verifier_rejects_a_recorded_009_migration_with_missing_columns(
    tmp_path: Path,
) -> None:
    application = _app(tmp_path)
    with TestClient(application):
        pass
    damaged = tmp_path / "damaged.sqlite"
    create_backup(application.state.database.path, damaged)

    connection = sqlite3.connect(damaged)
    try:
        # Simulate a truncated/tampered migration by rebuilding conversations
        # without one of the columns guaranteed by migration 009.
        connection.execute("ALTER TABLE conversations RENAME TO conversations_full")
        connection.execute(
            """
            CREATE TABLE conversations AS
            SELECT id, user_id, is_default, version, created_at, updated_at,
                   title, creation_key_hash
            FROM conversations_full
            """
        )
        connection.execute("DROP TABLE conversations_full")
        connection.commit()
    finally:
        connection.close()

    with pytest.raises(SQLiteMaintenanceError, match="creation_request_hash"):
        verify_database(damaged)


def test_database_repairs_private_permissions_without_touching_parent(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "private.sqlite"
    database = Database(
        database_path,
        clock=FrozenClock(datetime(2026, 8, 20, 1, 0, tzinfo=timezone.utc)),
    )
    original_parent_mode = stat.S_IMODE(tmp_path.stat().st_mode)
    database.initialize()

    assert stat.S_IMODE(database_path.stat().st_mode) == 0o600
    assert stat.S_IMODE(tmp_path.stat().st_mode) == original_parent_mode

    # Repair a database left by an older release without relying on a broad
    # recursive chmod. A normal connection is the common safety boundary.
    database_path.chmod(0o644)
    connection = database.connect()
    connection.close()
    assert stat.S_IMODE(database_path.stat().st_mode) == 0o600

    with database.transaction() as connection:
        connection.execute(
            "UPDATE users SET locale = locale WHERE id = ?",
            (database.default_user_id,),
        )
    for suffix in ("-wal", "-shm"):
        sidecar = Path(f"{database_path}{suffix}")
        if sidecar.exists():
            assert stat.S_IMODE(sidecar.stat().st_mode) == 0o600
