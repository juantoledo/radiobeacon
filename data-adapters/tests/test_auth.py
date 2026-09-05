import sqlite3

import pytest
from adapters.auth import (
    bootstrap_admin_if_missing,
    create_session,
    create_user,
    delete_all_sessions_for_user,
    delete_session,
    delete_user,
    get_user,
    get_user_by_username,
    hash_password,
    list_users,
    set_user_password,
    set_user_role,
    validate_session,
    verify_password,
)


@pytest.fixture
def conn():
    from adapters.storage import get_connection

    connection = get_connection(":memory:")
    yield connection
    connection.close()


def test_hash_and_verify_password_roundtrip():
    encoded = hash_password("correct horse battery staple")
    assert verify_password("correct horse battery staple", encoded)
    assert not verify_password("wrong password", encoded)


def test_verify_password_never_raises_on_malformed_hash():
    assert not verify_password("anything", "not-a-real-hash")
    assert not verify_password("anything", "")
    assert not verify_password("anything", "bcrypt$foo$bar")


def test_create_and_get_user(conn: sqlite3.Connection):
    user = create_user(conn, "alice", "hunter2", "admin", actor="test")
    assert user.username == "alice"
    assert user.role == "admin"
    assert not user.disabled

    fetched = get_user_by_username(conn, "alice")
    assert fetched == user
    assert get_user(conn, user.id) == user


def test_create_user_duplicate_username_raises(conn: sqlite3.Connection):
    create_user(conn, "alice", "hunter2", "admin", actor="test")
    with pytest.raises(sqlite3.IntegrityError):
        create_user(conn, "alice", "different", "user", actor="test")


def test_create_user_rejects_invalid_role(conn: sqlite3.Connection):
    with pytest.raises(ValueError):
        create_user(conn, "alice", "hunter2", "superadmin", actor="test")


def test_list_users_ordered_by_username(conn: sqlite3.Connection):
    create_user(conn, "bob", "pw", "user", actor="test")
    create_user(conn, "alice", "pw", "admin", actor="test")
    usernames = [u.username for u in list_users(conn)]
    assert usernames == ["alice", "bob"]


def test_set_user_password_invalidates_existing_sessions(conn: sqlite3.Connection):
    user = create_user(conn, "alice", "hunter2", "admin", actor="test")
    token = create_session(conn, user.id)
    assert validate_session(conn, token) is not None

    assert set_user_password(conn, user.id, "new-password", actor="test")
    assert validate_session(conn, token) is None
    # New password verifies against the stored hash.
    row = conn.execute("SELECT password_hash FROM users WHERE id = ?", (user.id,)).fetchone()
    assert verify_password("new-password", row[0])


def test_set_user_role_invalidates_existing_sessions(conn: sqlite3.Connection):
    user = create_user(conn, "alice", "hunter2", "admin", actor="test")
    token = create_session(conn, user.id)
    assert set_user_role(conn, user.id, "user", actor="test")
    assert validate_session(conn, token) is None
    assert get_user(conn, user.id).role == "user"


def test_delete_user_removes_sessions_too(conn: sqlite3.Connection):
    user = create_user(conn, "alice", "hunter2", "admin", actor="test")
    token = create_session(conn, user.id)
    assert delete_user(conn, user.id, actor="test")
    assert get_user(conn, user.id) is None
    assert validate_session(conn, token) is None
    assert delete_user(conn, user.id, actor="test") is False


def test_session_create_and_validate(conn: sqlite3.Connection):
    user = create_user(conn, "alice", "hunter2", "admin", actor="test")
    token = create_session(conn, user.id)
    validated = validate_session(conn, token)
    assert validated is not None
    assert validated.id == user.id


def test_validate_session_rejects_tampered_token(conn: sqlite3.Connection):
    user = create_user(conn, "alice", "hunter2", "admin", actor="test")
    create_session(conn, user.id)
    assert validate_session(conn, "not-a-real-token") is None


def test_validate_session_rejects_disabled_user(conn: sqlite3.Connection):
    user = create_user(conn, "alice", "hunter2", "admin", actor="test")
    token = create_session(conn, user.id)
    conn.execute("UPDATE users SET disabled = 1 WHERE id = ?", (user.id,))
    conn.commit()
    assert validate_session(conn, token) is None


def test_validate_session_rejects_expired_session(conn: sqlite3.Connection):
    user = create_user(conn, "alice", "hunter2", "admin", actor="test")
    token = create_session(conn, user.id)
    conn.execute(
        "UPDATE sessions SET expires_at = datetime('now', '-1 second') WHERE user_id = ?",
        (user.id,),
    )
    conn.commit()
    assert validate_session(conn, token) is None


def test_delete_session(conn: sqlite3.Connection):
    user = create_user(conn, "alice", "hunter2", "admin", actor="test")
    token = create_session(conn, user.id)
    delete_session(conn, token)
    assert validate_session(conn, token) is None


def test_delete_all_sessions_for_user(conn: sqlite3.Connection):
    user = create_user(conn, "alice", "hunter2", "admin", actor="test")
    token_a = create_session(conn, user.id)
    token_b = create_session(conn, user.id)
    delete_all_sessions_for_user(conn, user.id)
    assert validate_session(conn, token_a) is None
    assert validate_session(conn, token_b) is None


def test_bootstrap_admin_if_missing_creates_once(conn: sqlite3.Connection):
    created = bootstrap_admin_if_missing(conn)
    assert created is not None
    username, password = created
    assert username == "admin"
    assert len(password) > 0
    admin = get_user_by_username(conn, "admin")
    assert admin is not None
    assert admin.role == "admin"
    assert verify_password(password, conn.execute(
        "SELECT password_hash FROM users WHERE id = ?", (admin.id,)
    ).fetchone()[0])

    # Second call is a no-op — an admin already exists.
    assert bootstrap_admin_if_missing(conn) is None


def test_bootstrap_admin_if_missing_skips_when_only_disabled_admin_exists(conn: sqlite3.Connection):
    """A disabled admin still counts as "an admin exists" for bootstrap
    purposes — otherwise every restart would retry create_user(...,
    "admin", ...) and crash on the username's UNIQUE constraint instead of
    quietly no-opping (see any_admin_exists's docstring)."""
    admin = create_user(conn, "admin", "whatever", "admin", actor="test")
    conn.execute("UPDATE users SET disabled = 1 WHERE id = ?", (admin.id,))
    conn.commit()
    assert bootstrap_admin_if_missing(conn) is None
