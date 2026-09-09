"""The read-only 'user' role can browse items + audio but not mutate them.

Companion to test_users_routes.py's admin-only-route matrix: the items
router is now require_role("user") at the router level, with the four
mutating POST routes individually re-gated to admin.
"""


def _insert_item(conn, source, item_id):
    conn.execute(
        "INSERT INTO items (source, item_id, extracted_title, fetched_at, policy, rawdata) "
        "VALUES (?, ?, 'Title', datetime('now'), 'informational', '{}')",
        (source, item_id),
    )
    conn.commit()


def test_user_can_list_items(user_client):
    assert user_client.get("/items").status_code == 200


def test_user_can_view_item_detail_without_action_forms(user_client, conn):
    _insert_item(conn, "csn", "1")
    response = user_client.get("/items/csn/1")
    assert response.status_code == 200
    assert 'action="/items/csn/1/override"' not in response.text
    assert 'action="/items/csn/1/rearm"' not in response.text
    assert 'action="/items/csn/1/replay"' not in response.text
    assert 'action="/items/csn/1/retransmit"' not in response.text


def test_user_can_reach_item_audio_endpoint(user_client, conn):
    _insert_item(conn, "csn", "1")
    # 404 (no clip rendered) is fine — the point is it isn't 403.
    assert user_client.get("/items/csn/1/audio").status_code != 403


def test_user_cannot_mutate_items(user_client, conn):
    _insert_item(conn, "csn", "1")
    assert user_client.post("/items/csn/1/override", data={"policy": "informational"}).status_code == 403
    assert user_client.post("/items/csn/1/rearm", data={"consumer": "log"}).status_code == 403
    assert (
        user_client.post(
            "/items/csn/1/replay", data={"event_type": "item.ready", "consumer": "log"}
        ).status_code
        == 403
    )
    assert user_client.post("/items/csn/1/retransmit").status_code == 403


def test_user_sees_items_nav_link_but_not_admin_links(user_client):
    body = user_client.get("/").text
    assert 'href="/items"' in body
    assert 'href="/config"' not in body
    assert 'href="/audit"' not in body
    assert 'href="/users"' not in body


def test_admin_still_has_item_action_forms(client, conn):
    _insert_item(conn, "csn", "1")
    body = client.get("/items/csn/1").text
    assert 'action="/items/csn/1/override"' in body
    assert 'action="/items/csn/1/rearm"' in body
