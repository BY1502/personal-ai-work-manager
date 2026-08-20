from __future__ import annotations

import hashlib
import sqlite3
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path

from fastapi.testclient import TestClient

from app.auth import (
    SESSION_COOKIE_NAME,
    AuthService,
    RegisterRequest,
)
from app.database import Database
from app.extraction import DeterministicTestProvider
from app.main import create_app
from app.models import JarvisResponse
from app.repository import WorkRepository
from app.sqlite_maintenance import create_backup, restore_database, verify_database
from app.utils import FrozenClock, utc_iso


PASSWORD = "correct horse battery staple"


def _app(tmp_path: Path, *, clock: FrozenClock | None = None):
    resolved_clock = clock or FrozenClock(
        datetime(2026, 8, 20, 1, 0, tzinfo=timezone.utc)
    )
    return create_app(
        database_path=tmp_path / "auth.sqlite",
        clock=resolved_clock,
        extractor=DeterministicTestProvider(),
        auth_required=True,
    )


def _register(
    client: TestClient,
    username: str,
    *,
    password: str = PASSWORD,
) -> dict:
    response = client.post(
        "/api/v1/auth/register",
        json={"username": username, "password": password},
    )
    assert response.status_code == 201, response.text
    return response.json()


def _complete_message(
    repository: WorkRepository,
    *,
    user_id: str,
    conversation_id: str | None,
    client_message_id: str,
    content: str,
    reply: str,
):
    intake = repository.create_or_get_run(
        user_id=user_id,
        conversation_id=conversation_id,
        client_message_id=client_message_id,
        content=content,
    )
    response = JarvisResponse(
        run_id=intake.run_id,
        conversation_id=intake.conversation_id,
        status="COMPLETED",
        display_response=reply,
        voice_response=reply,
    )
    repository.complete_run(
        user_id=user_id,
        run_id=intake.run_id,
        response=response,
        memory_status="READ_ONLY",
    )
    return intake


def test_business_api_requires_session_and_cookie_is_server_side(tmp_path: Path) -> None:
    application = _app(tmp_path)
    with TestClient(application) as client:
        assert client.get("/health").status_code == 200
        denied = client.get("/api/v1/dashboard/summary")
        assert denied.status_code == 401
        assert denied.json()["error"]["code"] == "AUTHENTICATION_REQUIRED"
        assert "no-store" in denied.headers["cache-control"]

        registered = _register(client, "owner")
        assert registered["legacy_data_claimed"] is True
        cookie = client.cookies.get(SESSION_COOKIE_NAME)
        assert cookie

        login = client.post(
            "/api/v1/auth/login",
            json={"username": "owner", "password": PASSWORD},
        )
        rotated_cookie = client.cookies.get(SESSION_COOKIE_NAME)
        assert rotated_cookie and rotated_cookie != cookie
        set_cookie = login.headers["set-cookie"].casefold()
        assert "httponly" in set_cookie
        assert "samesite=lax" in set_cookie
        assert "path=/" in set_cookie

    connection = sqlite3.connect(application.state.database.path)
    try:
        password_credential = connection.execute(
            "SELECT password_credential FROM users WHERE normalized_username = 'owner'"
        ).fetchone()[0]
        sessions = {
            row[0]: row[1]
            for row in connection.execute(
                "SELECT token_hash, revoked_at FROM auth_sessions"
            )
        }
    finally:
        connection.close()
    assert PASSWORD not in password_credential
    assert cookie not in sessions
    old_hash = hashlib.sha256(cookie.encode("utf-8")).hexdigest()
    assert old_hash in sessions
    assert sessions[old_hash] is not None


def test_login_rotates_untrusted_cookie_and_logout_revokes_session(tmp_path: Path) -> None:
    application = _app(tmp_path)
    with TestClient(application) as bootstrap:
        _register(bootstrap, "owner")

    fixed = "attacker-controlled-session"
    with TestClient(application) as client:
        client.cookies.set(
            SESSION_COOKIE_NAME,
            fixed,
            domain="testserver.local",
            path="/",
        )
        response = client.post(
            "/api/v1/auth/login",
            json={"username": "owner", "password": PASSWORD},
        )
        assert response.status_code == 200
        issued = client.cookies.get(SESSION_COOKIE_NAME)
        assert issued and issued != fixed
        assert client.get("/api/v1/auth/me").status_code == 200

        csrf = client.post(
            "/api/v1/auth/logout",
            headers={"Origin": "https://attacker.example"},
        )
        assert csrf.status_code == 403
        assert client.get("/api/v1/auth/me").status_code == 200

        logout = client.post("/api/v1/auth/logout")
        assert logout.status_code == 204
        assert client.get("/api/v1/auth/me").status_code == 401

    with TestClient(application) as attacker:
        attacker.cookies.set(
            SESSION_COOKIE_NAME,
            fixed,
            domain="testserver.local",
            path="/",
        )
        assert attacker.get("/api/v1/auth/me").status_code == 401
        attacker.cookies.set(
            SESSION_COOKIE_NAME,
            issued,
            domain="testserver.local",
            path="/",
        )
        assert attacker.get("/api/v1/auth/me").status_code == 401


def test_expired_session_is_rejected(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("AUTH_SESSION_TTL_SECONDS", "60")
    clock = FrozenClock(datetime(2026, 8, 20, 1, 0, tzinfo=timezone.utc))
    application = _app(tmp_path, clock=clock)
    with TestClient(application) as client:
        _register(client, "owner")
        assert client.get("/api/v1/auth/me").status_code == 200
        clock.set(clock.now_utc() + timedelta(seconds=61))
        assert client.get("/api/v1/auth/me").status_code == 401


def test_invalid_login_duplicate_username_and_calendar_owner_boundary(
    tmp_path: Path,
) -> None:
    application = _app(tmp_path)
    with TestClient(application) as owner:
        _register(owner, "Owner.Name")
        invalid = owner.post(
            "/api/v1/auth/login",
            json={"username": "Owner.Name", "password": "wrong-password"},
        )
        assert invalid.status_code == 401
        assert invalid.json()["error"]["code"] == "INVALID_CREDENTIALS"

        duplicate = owner.post(
            "/api/v1/auth/register",
            json={"username": "owner.name", "password": PASSWORD},
        )
        assert duplicate.status_code == 409
        assert duplicate.json()["error"]["code"] == "USERNAME_TAKEN"

        with TestClient(application) as member:
            _register(member, "member")
            assert member.get("/api/v1/calendar/status").status_code == 403
            calendar_chat = member.post(
                "/api/v1/chat/runs",
                json={
                    "client_message_id": "member-calendar-attempt",
                    "content": "내일 오전 10시에 회의 일정 등록해줘.",
                },
            )
            assert calendar_chat.status_code == 403


def test_concurrent_first_registration_claims_legacy_data_once(tmp_path: Path) -> None:
    database = Database(
        tmp_path / "race.sqlite",
        clock=FrozenClock(datetime(2026, 8, 20, 1, 0, tzinfo=timezone.utc)),
    )
    database.initialize()
    auth = AuthService(database)
    barrier = threading.Barrier(2)
    results: list[tuple[str, bool, str]] = []
    errors: list[BaseException] = []

    def register(username: str) -> None:
        try:
            barrier.wait(timeout=3)
            session, claimed = auth.register(
                RegisterRequest(username=username, password=PASSWORD),
                user_agent="security-regression",
            )
            results.append((username, claimed, session.user.id))
        except BaseException as error:  # captured and asserted in the main thread
            errors.append(error)

    threads = [
        threading.Thread(target=register, args=("owner-a",)),
        threading.Thread(target=register, args=("owner-b",)),
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)

    assert not errors
    assert len(results) == 2
    assert sum(1 for _, claimed, _ in results if claimed) == 1
    assert sum(1 for _, _, user_id in results if user_id == "local-user") == 1
    connection = database.connect()
    try:
        assert connection.execute(
            "SELECT COUNT(*) FROM users WHERE is_owner = 1"
        ).fetchone()[0] == 1
    finally:
        connection.close()


def test_conversation_run_and_project_ids_are_owned_by_user(tmp_path: Path) -> None:
    application = _app(tmp_path)
    with TestClient(application) as owner:
        owner_user = _register(owner, "owner")["user"]
        owner_intake = _complete_message(
            application.state.repository,
            user_id=owner_user["id"],
            conversation_id=None,
            client_message_id="owner-message",
            content="owner secret work",
            reply="owner secret reply",
        )
        stamp = utc_iso(application.state.database.clock.now_utc())
        with application.state.database.transaction() as connection:
            connection.execute(
                """
                INSERT INTO projects(
                    id, user_id, name, normalized_name, created_at, updated_at
                ) VALUES ('owner-project', ?, 'Owner Project', 'ownerproject', ?, ?)
                """,
                (owner_user["id"], stamp, stamp),
            )

        with TestClient(application) as member:
            member_user = _register(member, "member")["user"]
            member_intake = _complete_message(
                application.state.repository,
                user_id=member_user["id"],
                conversation_id=None,
                client_message_id="member-message",
                content="member work",
                reply="member reply",
            )

            assert member.get(
                f"/api/v1/chat/conversations/{owner_intake.conversation_id}/messages"
            ).status_code == 404
            assert member.get(
                f"/api/v1/runs/{owner_intake.run_id}"
            ).status_code == 404
            assert member.get(
                "/api/v1/dashboard/projects/owner-project"
            ).status_code == 404
            cross_write = member.post(
                "/api/v1/chat/runs",
                json={
                    "conversation_id": owner_intake.conversation_id,
                    "client_message_id": "cross-user-write",
                    "content": "try another user's conversation",
                },
            )
            assert cross_write.status_code == 404

            own_history = member.get(
                f"/api/v1/chat/conversations/{member_intake.conversation_id}/messages"
            )
            assert own_history.status_code == 200
            assert [item["content"] for item in own_history.json()["items"]] == [
                "member work",
                "member reply",
            ]


def test_concurrent_chat_requests_never_swap_request_scoped_users(
    tmp_path: Path,
) -> None:
    application = _app(tmp_path)
    with TestClient(application) as owner, TestClient(application) as member:
        owner_user = _register(owner, "owner")["user"]
        member_user = _register(member, "member")["user"]
        barrier = threading.Barrier(2)
        results: list[tuple[str, int, dict]] = []

        def capture(label: str, client: TestClient) -> None:
            barrier.wait(timeout=3)
            response = client.post(
                "/api/v1/chat/runs",
                json={
                    "client_message_id": f"concurrent-{label}",
                    "content": "오늘 예측매니저 설치 가이드 수정했어.",
                },
            )
            results.append((label, response.status_code, response.json()))

        threads = [
            threading.Thread(target=capture, args=("owner", owner)),
            threading.Thread(target=capture, args=("member", member)),
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=15)

        assert len(results) == 2
        assert all(status == 200 for _, status, _ in results), results
        owner_projects = owner.get("/api/v1/dashboard/projects").json()["items"]
        member_projects = member.get("/api/v1/dashboard/projects").json()["items"]
        assert [item["name"] for item in owner_projects] == ["예측매니저"]
        assert [item["name"] for item in member_projects] == ["예측매니저"]
        assert owner_projects[0]["id"] != member_projects[0]["id"]

        connection = application.state.database.connect()
        try:
            owners = {
                row[0]
                for row in connection.execute(
                    "SELECT DISTINCT user_id FROM projects WHERE name = '예측매니저'"
                )
            }
        finally:
            connection.close()
        assert owners == {owner_user["id"], member_user["id"]}


def test_history_reconstructs_stable_user_assistant_order_after_refresh(
    tmp_path: Path,
) -> None:
    application = _app(tmp_path)
    with TestClient(application) as client:
        user = _register(client, "owner")["user"]
        first = _complete_message(
            application.state.repository,
            user_id=user["id"],
            conversation_id=None,
            client_message_id="message-1",
            content="first question",
            reply="first answer",
        )
        _complete_message(
            application.state.repository,
            user_id=user["id"],
            conversation_id=first.conversation_id,
            client_message_id="message-2",
            content="second question",
            reply="second answer",
        )

        history = client.get(
            f"/api/v1/chat/conversations/{first.conversation_id}/messages"
        )
        assert history.status_code == 200
        items = history.json()["items"]
        assert [item["role"] for item in items] == [
            "user",
            "assistant",
            "user",
            "assistant",
        ]
        assert [item["content"] for item in items] == [
            "first question",
            "first answer",
            "second question",
            "second answer",
        ]
        assert [item["sequence_cursor"] for item in items] == sorted(
            item["sequence_cursor"] for item in items
        )
        assert [
            item["client_message_id"] for item in items if item["role"] == "user"
        ] == ["message-1", "message-2"]
        assert all(item["response"] for item in items if item["role"] == "assistant")
        assert "no-store" in history.headers["cache-control"]


def test_provider_loading_status_does_not_leak_between_users(tmp_path: Path) -> None:
    class LocalProvider(DeterministicTestProvider):
        provider_name = "local"
        model_name = "security-test-model"

    application = create_app(
        database_path=tmp_path / "provider.sqlite",
        clock=FrozenClock(datetime(2026, 8, 20, 1, 0, tzinfo=timezone.utc)),
        extractor=LocalProvider(),
        auth_required=True,
    )
    with TestClient(application) as owner:
        owner_user = _register(owner, "owner")["user"]
        intake = application.state.repository.create_or_get_run(
            user_id=owner_user["id"],
            conversation_id=None,
            client_message_id="loading",
            content="provider loading",
        )
        application.state.repository.begin_interpretation(
            user_id=owner_user["id"],
            run_id=intake.run_id,
            provider_name="local",
            model_version="security-test-model",
            prompt_version="test",
        )
        assert owner.get("/api/v1/dashboard/provider").json()["state"] == "LOADING"

        with TestClient(application) as member:
            _register(member, "member")
            assert member.get("/api/v1/dashboard/provider").json()["state"] == "READY"


def test_pre_auth_backup_restores_then_upgrades_and_post_auth_backup_keeps_rows(
    tmp_path: Path,
) -> None:
    migrations = Path(__file__).parents[1] / "app" / "migrations"
    old_migrations = tmp_path / "old-migrations"
    old_migrations.mkdir()
    for migration in sorted(migrations.glob("*.sql")):
        if migration.name.startswith(("008_", "009_")):
            continue
        (old_migrations / migration.name).write_bytes(migration.read_bytes())

    clock = FrozenClock(datetime(2026, 8, 20, 1, 0, tzinfo=timezone.utc))
    legacy = Database(tmp_path / "legacy.sqlite", clock=clock)
    legacy.migrations_path = old_migrations
    legacy.initialize()
    legacy_backup = tmp_path / "legacy-backup.sqlite"
    create_backup(legacy.path, legacy_backup)
    assert "008" not in verify_database(legacy_backup).migrations
    assert "009" not in verify_database(legacy_backup).migrations

    upgraded_path = tmp_path / "upgraded.sqlite"
    restore_database(legacy_backup, upgraded_path)
    upgraded = Database(upgraded_path, clock=clock)
    upgraded.initialize()
    assert "008" in verify_database(upgraded_path).migrations
    assert "009" in verify_database(upgraded_path).migrations

    issued, claimed = AuthService(upgraded).register(
        RegisterRequest(username="owner", password=PASSWORD),
        user_agent="backup-regression",
    )
    assert claimed is True
    auth_backup = tmp_path / "auth-backup.sqlite"
    create_backup(upgraded_path, auth_backup)
    restored_path = tmp_path / "restored-auth.sqlite"
    restore_database(auth_backup, restored_path)
    restored = Database(restored_path, clock=clock)
    restored.initialize()
    connection = restored.connect()
    try:
        assert connection.execute(
            "SELECT COUNT(*) FROM users WHERE password_credential IS NOT NULL"
        ).fetchone()[0] == 1
        session = connection.execute(
            "SELECT revoked_at FROM auth_sessions"
        ).fetchone()
        assert session is not None
        assert session[0] is not None
    finally:
        connection.close()
    assert AuthService(restored).authenticate(issued.token) is None
