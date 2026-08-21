import json
import sqlite3
import threading
from typing import Any

import paho.mqtt.client

import actions.__main__ as main_module
from actions.base import Action


class FakeAction(Action):
    """Test double whose behavior is set per-test via class attributes,
    mirroring FakeAdapter in data-adapters/tests/test_scheduler.py."""

    outputs: list[dict[str, Any]] = []
    raises: bool = False
    run_calls: int = 0

    def run(self, event, *, conn):
        type(self).run_calls += 1
        if type(self).raises:
            raise RuntimeError("boom")
        return type(self).outputs


# _env_name derives the env var from the action's module name — fake a
# module path so it doesn't collide with any real action's vars.
FakeAction.__module__ = "actions.fakeaction"


class FakeMessage:
    def __init__(self, topic: str, payload: bytes):
        self.topic = topic
        self.payload = payload


class FakeClient:
    def __init__(self):
        self.published = []

    def publish(self, topic, payload=None, qos=0):
        self.published.append((topic, payload, qos))


def _dispatched_payload(source="senapred", item_id="1"):
    return json.dumps(
        {
            "specversion": "1.0",
            "type": "cl.radiobeacon.item.dispatched",
            "source": "radiobeacon/log_handler",
            "data": {"source": source, "item_id": item_id},
        }
    ).encode("utf-8")


def test_subscribe_topics_reads_action_specific_env_var(monkeypatch):
    monkeypatch.setenv(
        "ACTIONS_FAKEACTION_SUBSCRIBE_TOPIC", "radiobeacon/events/item.dispatched"
    )

    assert main_module._subscribe_topics(FakeAction) == ["radiobeacon/events/item.dispatched"]


def test_subscribe_topics_splits_comma_separated_list(monkeypatch):
    monkeypatch.setenv(
        "ACTIONS_FAKEACTION_SUBSCRIBE_TOPIC",
        "radiobeacon/events/item.dispatched, radiobeacon/events/item.reprocess",
    )

    assert main_module._subscribe_topics(FakeAction) == [
        "radiobeacon/events/item.dispatched",
        "radiobeacon/events/item.reprocess",
    ]


def test_subscribe_topics_returns_none_when_unset(monkeypatch):
    monkeypatch.delenv("ACTIONS_FAKEACTION_SUBSCRIBE_TOPIC", raising=False)

    assert main_module._subscribe_topics(FakeAction) is None


def test_output_config_returns_none_topic_and_default_event_type_when_unset(monkeypatch):
    monkeypatch.delenv("ACTIONS_FAKEACTION_OUTPUT_TOPIC", raising=False)
    monkeypatch.delenv("ACTIONS_FAKEACTION_OUTPUT_EVENT_TYPE", raising=False)

    assert main_module._output_config(FakeAction) == (None, "fakeaction")


def test_output_config_reads_action_specific_env_vars(monkeypatch):
    monkeypatch.setenv("ACTIONS_FAKEACTION_OUTPUT_TOPIC", "radiobeacon/events/item.chunked")
    monkeypatch.setenv("ACTIONS_FAKEACTION_OUTPUT_EVENT_TYPE", "item.chunked")

    assert main_module._output_config(FakeAction) == (
        "radiobeacon/events/item.chunked",
        "item.chunked",
    )


def test_on_message_records_audit_event_on_success(tmp_path, monkeypatch):
    monkeypatch.setattr(main_module, "DEFAULT_DB_PATH", tmp_path / "radiobeacon.db")
    FakeAction.outputs = []
    FakeAction.raises = False

    handler = main_module._make_on_message(FakeAction, None, "fakeaction")
    handler(FakeClient(), None, FakeMessage("radiobeacon/events/item.dispatched", _dispatched_payload()))

    conn = sqlite3.connect(tmp_path / "radiobeacon.db")
    row = conn.execute(
        "SELECT event_type, actor, source, item_id FROM audit_log WHERE event_type = 'action.fakeaction.executed'"
    ).fetchone()
    assert row == ("action.fakeaction.executed", "actions.fakeaction", "senapred", "1")


def test_on_message_swallows_exceptions_and_logs(tmp_path, monkeypatch):
    monkeypatch.setattr(main_module, "DEFAULT_DB_PATH", tmp_path / "radiobeacon.db")
    FakeAction.outputs = []
    FakeAction.raises = True

    handler = main_module._make_on_message(FakeAction, None, "fakeaction")

    # must not raise — a raised exception here would silently kill this
    # action's paho network thread in loop_start() mode
    handler(FakeClient(), None, FakeMessage("radiobeacon/events/item.dispatched", _dispatched_payload()))


def test_on_message_swallows_malformed_payload(tmp_path, monkeypatch):
    monkeypatch.setattr(main_module, "DEFAULT_DB_PATH", tmp_path / "radiobeacon.db")
    FakeAction.outputs = []
    FakeAction.raises = False

    handler = main_module._make_on_message(FakeAction, None, "fakeaction")

    handler(FakeClient(), None, FakeMessage("radiobeacon/events/item.dispatched", b"not json"))


def test_on_message_publishes_one_cloud_event_per_output_item(tmp_path, monkeypatch):
    monkeypatch.setattr(main_module, "DEFAULT_DB_PATH", tmp_path / "radiobeacon.db")
    FakeAction.outputs = [{"text": "chunk one"}, {"text": "chunk two"}]
    FakeAction.raises = False

    handler = main_module._make_on_message(
        FakeAction, "radiobeacon/events/item.chunked", "item.chunked"
    )
    client = FakeClient()
    handler(client, None, FakeMessage("radiobeacon/events/item.dispatched", _dispatched_payload()))

    assert len(client.published) == 2
    for topic, payload, qos in client.published:
        assert topic == "radiobeacon/events/item.chunked"
        body = json.loads(payload)
        assert body["type"] == "cl.radiobeacon.item.chunked"
        assert body["source"] == "radiobeacon/actions.fakeaction"


def test_on_message_skips_publish_when_no_output_topic_configured(tmp_path, monkeypatch):
    monkeypatch.setattr(main_module, "DEFAULT_DB_PATH", tmp_path / "radiobeacon.db")
    FakeAction.outputs = [{"text": "chunk one"}]
    FakeAction.raises = False

    handler = main_module._make_on_message(FakeAction, None, "fakeaction")
    client = FakeClient()
    handler(client, None, FakeMessage("radiobeacon/events/item.dispatched", _dispatched_payload()))

    assert client.published == []


def test_on_message_skips_already_processed_item(tmp_path, monkeypatch):
    monkeypatch.setattr(main_module, "DEFAULT_DB_PATH", tmp_path / "radiobeacon.db")
    FakeAction.outputs = [{"text": "chunk one"}]
    FakeAction.raises = False
    FakeAction.run_calls = 0

    handler = main_module._make_on_message(
        FakeAction, "radiobeacon/events/item.chunked", "item.chunked"
    )
    client = FakeClient()
    message = FakeMessage("radiobeacon/events/item.dispatched", _dispatched_payload())

    handler(client, None, message)  # first delivery — processes normally
    assert FakeAction.run_calls == 1
    assert len(client.published) == 1

    handler(client, None, message)  # a duplicate delivery of the same event

    assert FakeAction.run_calls == 1  # run() not invoked again
    assert len(client.published) == 1  # no additional publish


def test_on_message_processes_item_not_previously_seen(tmp_path, monkeypatch):
    monkeypatch.setattr(main_module, "DEFAULT_DB_PATH", tmp_path / "radiobeacon.db")
    FakeAction.outputs = []
    FakeAction.raises = False
    FakeAction.run_calls = 0

    handler = main_module._make_on_message(FakeAction, None, "fakeaction")
    handler(
        FakeClient(),
        None,
        FakeMessage("radiobeacon/events/item.dispatched", _dispatched_payload(item_id="1")),
    )
    handler(
        FakeClient(),
        None,
        FakeMessage("radiobeacon/events/item.dispatched", _dispatched_payload(item_id="2")),
    )

    assert FakeAction.run_calls == 2  # distinct item_ids — both processed


def test_already_processed_returns_false_without_source_or_item_id(tmp_path):
    conn = sqlite3.connect(tmp_path / "radiobeacon.db")
    conn.execute(
        "CREATE TABLE audit_log (event_type TEXT, source TEXT, item_id TEXT)"
    )

    assert main_module._already_processed(conn, "fakeaction", None, "1") is False
    assert main_module._already_processed(conn, "fakeaction", "senapred", None) is False


def test_run_action_loop_uses_stable_client_id_and_clean_session_false(monkeypatch):
    captured = {}

    class FakeClientForConnect:
        def __init__(self, callback_api_version, client_id=None, clean_session=None):
            captured["client_id"] = client_id
            captured["clean_session"] = clean_session

    monkeypatch.setattr(paho.mqtt.client, "Client", FakeClientForConnect)

    stop_event = threading.Event()
    stop_event.set()  # already stopped — client is constructed, then the
    # function returns before ever attempting to connect

    main_module._run_action_loop(
        FakeAction, ["radiobeacon/events/item.dispatched"], None, "fakeaction", stop_event
    )

    assert captured["client_id"] == "radiobeacon-actions-fakeaction"
    assert captured["clean_session"] is False
