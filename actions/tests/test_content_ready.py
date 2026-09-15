from adapters.storage import get_connection, record_audit_event

from actions import content_ready


class FakeClient:
    def __init__(self):
        self.published = []

    def publish(self, topic, payload=None, qos=0):
        self.published.append((topic, payload, qos))


def _make_conn(tmp_path):
    return get_connection(tmp_path / "radiobeacon.db")


def _store_item(conn, source="senapred", item_id="1", summary=None):
    conn.execute(
        "INSERT INTO items (source, item_id, extracted_contents, summary, fetched_at, rawdata) "
        "VALUES (?, ?, 'raw contents', ?, datetime('now'), '{}')",
        (source, item_id, summary),
    )
    conn.commit()


def _record_executed(conn, name, source, item_id, recorded_at=None):
    record_audit_event(
        conn, event_type=f"action.{name}.executed", actor=f"actions.{name}",
        source=source, item_id=item_id, details={},
    )
    if recorded_at is not None:
        # audit_log.recorded_at defaults to datetime('now'), which only
        # has second-level granularity — backdating explicitly here
        # avoids a flaky same-second tie against a later real write in
        # the same test.
        conn.execute(
            "UPDATE audit_log SET recorded_at = ? WHERE id = (SELECT MAX(id) FROM audit_log)",
            (recorded_at,),
        )
        conn.commit()


def test_check_and_publish_publishes_when_both_actions_done(tmp_path):
    conn = _make_conn(tmp_path)
    _store_item(conn, summary="a short summary")
    _record_executed(conn, "chunk", "senapred", "1")
    _record_executed(conn, "ai", "senapred", "1")

    client = FakeClient()
    published = content_ready.check_and_publish(
        conn, client, output_topic="radiobeacon/events/item.content_ready",
        actor="actions.content_ready", qos=1,
    )

    assert published == 1
    assert len(client.published) == 1
    topic, payload, qos = client.published[0]
    assert topic == "radiobeacon/events/item.content_ready"
    assert qos == 1
    assert '"item_id": "1"' in payload

    row = conn.execute(
        "SELECT event_type, source, item_id FROM audit_log WHERE event_type = 'item.content_ready.published'"
    ).fetchone()
    assert row == ("item.content_ready.published", "senapred", "1")


def test_check_and_publish_skips_when_only_one_action_done(tmp_path):
    conn = _make_conn(tmp_path)
    _store_item(conn)
    _record_executed(conn, "chunk", "senapred", "1")  # ai never fired

    client = FakeClient()
    published = content_ready.check_and_publish(
        conn, client, output_topic="radiobeacon/events/item.content_ready",
        actor="actions.content_ready", qos=1,
    )

    assert published == 0
    assert client.published == []


def test_check_and_publish_does_not_republish_same_settlement(tmp_path):
    conn = _make_conn(tmp_path)
    _store_item(conn, summary="a short summary")
    _record_executed(conn, "chunk", "senapred", "1")
    _record_executed(conn, "ai", "senapred", "1")

    client = FakeClient()
    first = content_ready.check_and_publish(
        conn, client, output_topic="radiobeacon/events/item.content_ready",
        actor="actions.content_ready", qos=1,
    )
    second = content_ready.check_and_publish(
        conn, client, output_topic="radiobeacon/events/item.content_ready",
        actor="actions.content_ready", qos=1,
    )

    assert first == 1
    assert second == 0
    assert len(client.published) == 1


def test_check_and_publish_excludes_when_published_at_exactly_equals_settled_at(tmp_path):
    """Boundary case for the LEFT JOIN's `settled_at > published_at`
    filter: when the two are EXACTLY equal (not just close), the item
    must stay excluded — this is the previous per-row `settled_at <=
    published_at` skip-if-already-published behavior. A careless `>` vs
    `>=` inversion in the join's HAVING clause would silently republish
    here instead."""
    conn = _make_conn(tmp_path)
    _store_item(conn, summary="a short summary")
    _record_executed(conn, "chunk", "senapred", "1", recorded_at="2021-01-01 00:00:00")
    _record_executed(conn, "ai", "senapred", "1", recorded_at="2021-01-01 00:00:00")

    client = FakeClient()
    first = content_ready.check_and_publish(
        conn, client, output_topic="radiobeacon/events/item.content_ready",
        actor="actions.content_ready", qos=1,
    )
    assert first == 1

    # Force published_at to be EXACTLY equal to settled_at (rather than
    # merely close, which real "now" timestamps would only coincidentally
    # be) so the test pins down the exact boundary, not just same-second
    # granularity.
    conn.execute(
        "UPDATE item_readiness SET published_at = '2021-01-01 00:00:00' "
        "WHERE source = 'senapred' AND item_id = '1'"
    )
    conn.commit()

    second = content_ready.check_and_publish(
        conn, client, output_topic="radiobeacon/events/item.content_ready",
        actor="actions.content_ready", qos=1,
    )

    assert second == 0
    assert len(client.published) == 1


def test_check_and_publish_republishes_after_rearm(tmp_path):
    """A rearm doesn't delete prior audit rows, it produces new ones — a
    fresh action.chunk.executed/action.ai.executed pair after an earlier
    publish must trigger a fresh publish, not be silently skipped. The
    original pair is backdated so its settlement is unambiguously earlier
    than the rearm's fresh pair (recorded_at is second-granularity)."""
    conn = _make_conn(tmp_path)
    _store_item(conn, summary="a short summary")
    _record_executed(conn, "chunk", "senapred", "1", recorded_at="2020-01-01 00:00:00")
    _record_executed(conn, "ai", "senapred", "1", recorded_at="2020-01-01 00:00:00")

    client = FakeClient()
    content_ready.check_and_publish(
        conn, client, output_topic="radiobeacon/events/item.content_ready",
        actor="actions.content_ready", qos=1,
    )
    assert len(client.published) == 1

    # the first publish's item_readiness.published_at is real "now" (also
    # second-granularity) — backdate it too, so the rearm's fresh audit
    # rows (also real "now", moments later) can't tie with it in a fast
    # test run.
    conn.execute(
        "UPDATE item_readiness SET published_at = '2020-06-01 00:00:00' "
        "WHERE source = 'senapred' AND item_id = '1'"
    )
    conn.commit()

    # rearm: both actions run again, producing fresh audit rows — recorded_at
    # defaults to datetime('now'), well after the backdated published_at
    _record_executed(conn, "chunk", "senapred", "1")
    _record_executed(conn, "ai", "senapred", "1")

    published = content_ready.check_and_publish(
        conn, client, output_topic="radiobeacon/events/item.content_ready",
        actor="actions.content_ready", qos=1,
    )

    assert published == 1
    assert len(client.published) == 2


def test_aborted_item_never_becomes_content_ready(tmp_path, monkeypatch):
    """An item whose adapter has ai_on_failure=abort and no real summary:
    AiAction records item.ai_aborted and publishes nothing, so chunk never
    runs and the content_ready gate (needs both action.*.executed rows) is
    never met."""
    from adapters.storage import set_adapter_instance

    from actions.ai import AiAction

    monkeypatch.setenv("ACTIONS_AI_ENABLED", "false")
    conn = _make_conn(tmp_path)
    set_adapter_instance(
        conn, "src", "custom",
        {"code": "def fetch(config): return []", "ai_on_failure": "abort"},
    )
    conn.execute(
        "INSERT INTO items (source, item_id, extracted_contents, fetched_at, rawdata) "
        "VALUES ('src', '1', 'raw blob', datetime('now'), '{}')"
    )
    conn.commit()

    outputs = AiAction().run(
        {"type": "x", "data": {"source": "src", "item_id": "1"}}, conn=conn
    )
    assert outputs == []
    # simulate __main__ still writing the action.ai.executed row (output_count 0)
    _record_executed(conn, "ai", "src", "1")

    client = FakeClient()
    published = content_ready.check_and_publish(
        conn, client, output_topic="radiobeacon/events/item.content_ready",
        actor="actions.content_ready", qos=1,
    )
    assert published == 0
    assert client.published == []
