from adapters.auth import get_user_by_username


def test_users_list_page_loads(client):
    response = client.get("/users")
    assert response.status_code == 200
    assert "admin" in response.text


def test_create_user(client, conn):
    # No csrf_token needed — the shared `client` fixture overrides
    # verify_csrf to a no-op, same as every other route test.
    response = client.post(
        "/users",
        data={
            "username": "operator",
            "password": "correct-horse",
            "confirm_password": "correct-horse",
            "role": "user",
        },
        follow_redirects=False,
    )
    assert response.status_code == 303
    created = get_user_by_username(conn, "operator")
    assert created is not None
    assert created.role == "user"


def test_create_user_rejects_mismatched_passwords(client):
    response = client.post(
        "/users",
        data={
            "username": "operator2",
            "password": "correct-horse",
            "confirm_password": "does-not-match",
            "role": "user",
        },
        follow_redirects=False,
    )
    assert response.status_code == 400
    assert "do not match" in response.text


def test_create_user_rejects_duplicate_username(client):
    response = client.post(
        "/users",
        data={
            "username": "admin",
            "password": "correct-horse",
            "confirm_password": "correct-horse",
            "role": "user",
        },
        follow_redirects=False,
    )
    assert response.status_code == 400
    assert "already taken" in response.text


def test_change_role(client, conn):
    from adapters.auth import create_user

    user = create_user(conn, "operator", "correct-horse", "user", actor="test")
    response = client.post(
        f"/users/{user.id}/role", data={"role": "admin"}, follow_redirects=False
    )
    assert response.status_code == 303
    assert get_user_by_username(conn, "operator").role == "admin"


def test_reset_password(client, conn):
    from adapters.auth import create_user, verify_password

    user = create_user(conn, "operator", "old-password", "user", actor="test")
    response = client.post(
        f"/users/{user.id}/reset-password",
        data={"password": "new-password-123", "confirm_password": "new-password-123"},
        follow_redirects=False,
    )
    assert response.status_code == 303
    row = conn.execute("SELECT password_hash FROM users WHERE id = ?", (user.id,)).fetchone()
    assert verify_password("new-password-123", row[0])


def test_delete_user(client, conn):
    from adapters.auth import create_user

    user = create_user(conn, "operator", "correct-horse", "user", actor="test")
    response = client.post(f"/users/{user.id}/delete", follow_redirects=False)
    assert response.status_code == 303
    assert get_user_by_username(conn, "operator") is None


def test_cannot_delete_the_last_remaining_admin(client, conn, admin_user):
    response = client.post(f"/users/{admin_user.id}/delete", follow_redirects=False)
    assert response.status_code == 400
    assert "last remaining admin" in response.text
    assert get_user_by_username(conn, "admin") is not None


def test_cannot_demote_the_last_remaining_admin(client, admin_user):
    response = client.post(
        f"/users/{admin_user.id}/role", data={"role": "user"}, follow_redirects=False
    )
    assert response.status_code == 400
    assert "last remaining admin" in response.text


def test_deleting_a_second_admin_is_allowed(client, conn):
    from adapters.auth import create_user

    second_admin = create_user(conn, "root2", "correct-horse", "admin", actor="test")
    response = client.post(f"/users/{second_admin.id}/delete", follow_redirects=False)
    assert response.status_code == 303


def test_user_role_cannot_reach_admin_only_routes(user_client):
    assert user_client.get("/users", follow_redirects=False).status_code == 403
    assert user_client.get("/items", follow_redirects=False).status_code == 403
    assert user_client.get("/config", follow_redirects=False).status_code == 403
    assert user_client.get("/audit", follow_redirects=False).status_code == 403


def test_user_role_can_reach_dashboard(user_client):
    assert user_client.get("/").status_code == 200
