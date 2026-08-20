from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

from fastapi.testclient import TestClient

from app.auth import AuthService, LoginRequest, RegisterRequest
from app.database import Database
from app.extraction import DeterministicTestProvider
from app.main import create_app
from app.sqlite_maintenance import create_backup, restore_database
from app.utils import FrozenClock


PASSWORD = "correct horse battery staple"
NEW_PASSWORD = "a different secure password"


def _app(path: Path, clock: FrozenClock):
    return create_app(
        database_path=path,
        clock=clock,
        extractor=DeterministicTestProvider(),
        auth_required=True,
    )


def _register(client: TestClient, username: str = "owner") -> dict:
    response = client.post(
        "/api/v1/auth/register",
        json={"username": username, "password": PASSWORD},
    )
    assert response.status_code == 201, response.text
    return response.json()


def _login(client: TestClient, username: str, password: str = PASSWORD):
    return client.post(
        "/api/v1/auth/login",
        json={"username": username, "password": password},
    )


def test_password_change_rotates_session_and_revokes_other_devices(
    tmp_path: Path,
) -> None:
    clock = FrozenClock(datetime(2026, 8, 20, 1, 0, tzinfo=timezone.utc))
    application = _app(tmp_path / "change.sqlite", clock)
    with TestClient(application) as primary:
        _register(primary)
        with TestClient(application) as other:
            assert _login(other, "owner").status_code == 200
            old_primary_cookie = primary.cookies.get("by_session")

            reused = primary.post(
                "/api/v1/auth/password/change",
                json={
                    "current_password": PASSWORD,
                    "new_password": PASSWORD,
                },
            )
            assert reused.status_code == 422

            changed = primary.post(
                "/api/v1/auth/password/change",
                json={
                    "current_password": PASSWORD,
                    "new_password": NEW_PASSWORD,
                },
            )
            assert changed.status_code == 200
            assert changed.json()["recovery_code"].startswith("BYRC-")
            assert primary.cookies.get("by_session") != old_primary_cookie
            assert primary.get("/api/v1/auth/me").status_code == 200
            assert other.get("/api/v1/auth/me").status_code == 401
            assert _login(other, "owner", PASSWORD).status_code == 401
            assert _login(other, "owner", NEW_PASSWORD).status_code == 200


def test_recovery_code_is_one_time_hashed_and_reset_revokes_every_session(
    tmp_path: Path,
) -> None:
    clock = FrozenClock(datetime(2026, 8, 20, 1, 0, tzinfo=timezone.utc))
    application = _app(tmp_path / "recovery.sqlite", clock)
    with TestClient(application) as primary:
        registered = _register(primary)
        initial_code = registered["recovery_code"]
        assert initial_code.startswith("BYRC-")

        connection = sqlite3.connect(application.state.database.path)
        try:
            stored_hash = connection.execute(
                "SELECT recovery_code_hash FROM users WHERE id = 'local-user'"
            ).fetchone()[0]
        finally:
            connection.close()
        assert stored_hash and stored_hash != initial_code

        rotated = primary.post(
            "/api/v1/auth/recovery-code/rotate",
            json={"current_password": PASSWORD},
        )
        assert rotated.status_code == 200
        active_code = rotated.json()["recovery_code"]
        assert active_code != initial_code

        with TestClient(application) as other:
            assert _login(other, "owner").status_code == 200
            stale = other.post(
                "/api/v1/auth/password/reset",
                json={
                    "username": "owner",
                    "recovery_code": initial_code,
                    "new_password": NEW_PASSWORD,
                },
            )
            assert stale.status_code == 401

            reset = other.post(
                "/api/v1/auth/password/reset",
                json={
                    "username": "owner",
                    "recovery_code": active_code,
                    "new_password": NEW_PASSWORD,
                },
            )
            assert reset.status_code == 200
            replacement_code = reset.json()["recovery_code"]
            assert replacement_code not in {initial_code, active_code}
            assert primary.get("/api/v1/auth/me").status_code == 401
            assert other.get("/api/v1/auth/me").status_code == 401

            replay = other.post(
                "/api/v1/auth/password/reset",
                json={
                    "username": "owner",
                    "recovery_code": active_code,
                    "new_password": "yet another secure password",
                },
            )
            assert replay.status_code == 401
            assert _login(other, "owner", PASSWORD).status_code == 401
            assert _login(other, "owner", NEW_PASSWORD).status_code == 200


def test_login_rate_limit_is_persistent_hashed_and_clears_on_success(
    tmp_path: Path,
) -> None:
    clock = FrozenClock(datetime(2026, 8, 20, 1, 0, tzinfo=timezone.utc))
    database_path = tmp_path / "rate-limit.sqlite"
    application = _app(database_path, clock)
    with TestClient(application) as client:
        _register(client)
        client.post("/api/v1/auth/logout")
        for _ in range(4):
            assert _login(client, "owner", "wrong password").status_code == 401
        limited = _login(client, "owner", "wrong password")
        assert limited.status_code == 429
        assert limited.json()["error"]["code"] == "TOO_MANY_LOGIN_ATTEMPTS"
        assert limited.headers["retry-after"] == "900"
        assert _login(client, "owner", PASSWORD).status_code == 429

    connection = sqlite3.connect(database_path)
    try:
        columns = {
            row[1]
            for row in connection.execute("PRAGMA table_info(login_rate_limits)")
        }
        values = connection.execute(
            "SELECT identifier_hash FROM login_rate_limits"
        ).fetchall()
    finally:
        connection.close()
    assert "username" not in columns
    assert values and all("owner" not in value[0] for value in values)

    restarted = _app(database_path, clock)
    with TestClient(restarted) as client:
        assert _login(client, "owner", PASSWORD).status_code == 429
        clock.set(clock.now_utc() + timedelta(minutes=15, seconds=1))
        assert _login(client, "owner", PASSWORD).status_code == 200
    connection = sqlite3.connect(database_path)
    try:
        assert connection.execute(
            "SELECT COUNT(*) FROM login_rate_limits"
        ).fetchone()[0] == 0
    finally:
        connection.close()


def test_logout_all_revokes_all_sessions_and_clears_current_cookie(
    tmp_path: Path,
) -> None:
    clock = FrozenClock(datetime(2026, 8, 20, 1, 0, tzinfo=timezone.utc))
    application = _app(tmp_path / "logout-all.sqlite", clock)
    with TestClient(application) as primary:
        _register(primary)
        with TestClient(application) as other:
            assert _login(other, "owner").status_code == 200
            response = primary.post("/api/v1/auth/logout-all")
            assert response.status_code == 204
            assert primary.get("/api/v1/auth/me").status_code == 401
            assert other.get("/api/v1/auth/me").status_code == 401


def test_conversation_create_is_idempotent_and_user_scoped(tmp_path: Path) -> None:
    clock = FrozenClock(datetime(2026, 8, 20, 1, 0, tzinfo=timezone.utc))
    application = _app(tmp_path / "conversations.sqlite", clock)
    headers = {"Idempotency-Key": "new-conversation-001"}
    with TestClient(application) as owner:
        _register(owner)
        created = owner.post(
            "/api/v1/chat/conversations",
            headers=headers,
            json={"title": "업무 기록"},
        )
        assert created.status_code == 201
        owner_id = created.json()["conversation"]["id"]
        assert created.json()["created"] is True
        assert created.json()["conversation"]["title"] == "업무 기록"

        replay = owner.post(
            "/api/v1/chat/conversations",
            headers=headers,
            json={"title": "업무 기록"},
        )
        assert replay.status_code == 200
        assert replay.json()["conversation"]["id"] == owner_id
        assert replay.json()["created"] is False

        conflict = owner.post(
            "/api/v1/chat/conversations",
            headers=headers,
            json={"title": "다른 제목"},
        )
        assert conflict.status_code == 409

        with TestClient(application) as member:
            _register(member, "member")
            member_created = member.post(
                "/api/v1/chat/conversations",
                headers=headers,
                json={"title": "업무 기록"},
            )
            assert member_created.status_code == 201
            assert member_created.json()["conversation"]["id"] != owner_id
            assert member.get(
                f"/api/v1/chat/conversations/{owner_id}/messages"
            ).status_code == 404
            listed = member.get("/api/v1/chat/conversations").json()["items"]
            assert [item["id"] for item in listed] == [
                member_created.json()["conversation"]["id"]
            ]


def test_restore_revokes_sessions_and_invalidates_recovery_code(tmp_path: Path) -> None:
    clock = FrozenClock(datetime(2026, 8, 20, 1, 0, tzinfo=timezone.utc))
    source = Database(tmp_path / "source.sqlite", clock=clock)
    source.initialize()
    issued, _ = AuthService(source).register(
        RegisterRequest(username="owner", password=PASSWORD),
        user_agent="recovery-backup-test",
    )
    assert issued.recovery_code
    backup = tmp_path / "backup.sqlite"
    restored_path = tmp_path / "restored.sqlite"
    create_backup(source.path, backup)
    restore_database(backup, restored_path)

    restored = Database(restored_path, clock=clock)
    restored.initialize()
    connection = restored.connect()
    try:
        row = connection.execute(
            "SELECT recovery_code_hash FROM users WHERE id = 'local-user'"
        ).fetchone()
        assert row["recovery_code_hash"] is None
    finally:
        connection.close()
    service = AuthService(restored)
    assert service.authenticate(issued.token) is None
    logged_in = service.login(
        request=LoginRequest(
            username="owner",
            password=PASSWORD,
        ),
        user_agent="recovery-backup-test",
    )
    assert logged_in.user.id == "local-user"
    assert service.rotate_recovery_code(
        user_id=logged_in.user.id,
        current_password=PASSWORD,
    ).startswith("BYRC-")
