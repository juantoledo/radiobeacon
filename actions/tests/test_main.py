import json
import sqlite3
import threading
import uuid
from typing import Any

import paho.mqtt.client

import actions.__main__ as main_module
from actions.ai import AiAction
from actions.base import Action
from actions.chunk import ChunkAction


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


def _dispatched_payload(source="senapred", item_id="1", event_id=None):
    # A real id is essential here, not cosmetic: cloudevents' from_dict()
    # (used by mq.parse_cloud_event) auto-generates a fresh random id
    # whenever one is missing from the input dict — so an id-less payload
    # would get a NEW random id on every single parse, making even a
    # byte-for-byte-identical redelivery look like a distinct event to
    # _already_processed. Real payloads always carry one (assigned once,
    # at CloudEvent-construction time, by mq.build_cloud_event_payload /
    # dispatcher.mq_publisher.publish_cloud_event) — this fixture mirrors
    # that. Omit event_id to get a fresh one per call (two logically
    # different events, e.g. an original dispatch and a later rearm);
    # pass the same event_id explicitly to simulate a redelivered
    # duplicate of the exact same event.
    return json.dumps(
        {
            "specversion": "1.0",
            "type": "cl.radiobeacon.item.dispatched",
            "source": "radiobeacon/log_handler",
            "id": event_id or str(uuid.uuid4()),
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
    # FakeAction declares no default_subscribe_topic, so the skip-when-unset
    # contract still holds for an action that opts out of declarative wiring.
    monkeypatch.delenv("ACTIONS_FAKEACTION_SUBSCRIBE_TOPIC", raising=False)

    assert main_module._subscribe_topics(FakeAction) is None


def test_output_config_returns_none_topic_and_default_event_type_when_unset(monkeypatch):
    # Likewise: FakeAction declares no default_output_topic / _event_type.
    monkeypatch.delenv("ACTIONS_FAKEACTION_OUTPUT_TOPIC", raising=False)
    monkeypatch.delenv("ACTIONS_FAKEACTION_OUTPUT_EVENT_TYPE", raising=False)

    assert main_module._output_config(FakeAction) == (None, "fakeaction")


def test_subscribe_topics_falls_back_to_declared_class_default(monkeypatch):
    monkeypatch.delenv("ACTIONS_AI_SUBSCRIBE_TOPIC", raising=False)
    monkeypatch.delenv("ACTIONS_CHUNK_SUBSCRIBE_TOPIC", raising=False)

    assert main_module._subscribe_topics(AiAction) == ["radiobeacon/events/item.dispatched"]
    assert main_module._subscribe_topics(ChunkAction) == ["radiobeacon/events/item.ai_settled"]


def test_output_config_uses_declared_class_defaults(monkeypatch):
    for var in (
        "ACTIONS_AI_OUTPUT_TOPIC",
        "ACTIONS_AI_OUTPUT_EVENT_TYPE",
        "ACTIONS_CHUNK_OUTPUT_TOPIC",
        "ACTIONS_CHUNK_OUTPUT_EVENT_TYPE",
    ):
        monkeypatch.delenv(var, raising=False)

    assert main_module._output_config(AiAction) == (
        "radiobeacon/events/item.ai_settled",
        "item.ai_settled",
    )
    assert main_module._output_config(ChunkAction) == (
        "radiobeacon/events/item.chunked",
        "item.chunked",
    )


def test_env_var_overrides_declared_class_default(monkeypatch):
    monkeypatch.setenv("ACTIONS_AI_SUBSCRIBE_TOPIC", "radiobeacon/events/custom")
    monkeypatch.setenv("ACTIONS_AI_OUTPUT_TOPIC", "radiobeacon/events/custom_out")
    monkeypatch.setenv("ACTIONS_AI_OUTPUT_EVENT_TYPE", "custom.type")

    assert main_module._subscribe_topics(AiAction) == ["radiobeacon/events/custom"]
    assert main_module._output_config(AiAction) == (
        "radiobeacon/events/custom_out",
        "custom.type",
    )


def test_empty_string_still_disables_action_with_a_declared_default(monkeypatch):
    # The per-action kill switch: an explicit empty string wins over the
    # declared default, so get_setting returns "" and the action is skipped.
    monkeypatch.setenv("ACTIONS_AI_SUBSCRIBE_TOPIC", "")

    assert main_module._subscribe_topics(AiAction) is None


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


def test_on_message_copies_single_output_diagnostics_into_audit_details(tmp_path, monkeypatch):
    """A single-output action can attach diagnostic detail (ai's skip
    `reason`, the `provider`/`model` used, the rendered `prompt`) —
    __main__ mirrors it into the audit row so it shows up on /audit
    without cross-referencing logs. `summary` is NOT copied."""
    monkeypatch.setattr(main_module, "DEFAULT_DB_PATH", tmp_path / "radiobeacon.db")
    FakeAction.outputs = [
        {
            "source": "senapred",
            "item_id": "1",
            "summarized": True,
            "summary": "the stored summary",
            "provider": "ollama",
            "model": "llama3.2:1b",
            "prompt": "Resume esto: ...",
        }
    ]
    FakeAction.raises = False

    handler = main_module._make_on_message(FakeAction, None, "fakeaction")
    handler(FakeClient(), None, FakeMessage("radiobeacon/events/item.dispatched", _dispatched_payload()))

    conn = sqlite3.connect(tmp_path / "radiobeacon.db")
    details = conn.execute(
        "SELECT details FROM audit_log WHERE event_type = 'action.fakeaction.executed'"
    ).fetchone()[0]
    parsed = json.loads(details)
    assert parsed["provider"] == "ollama"
    assert parsed["model"] == "llama3.2:1b"
    assert parsed["prompt"] == "Resume esto: ..."
    assert "summary" not in parsed


def test_on_message_copies_single_output_reason_into_audit_details(tmp_path, monkeypatch):
    monkeypatch.setattr(main_module, "DEFAULT_DB_PATH", tmp_path / "radiobeacon.db")
    FakeAction.outputs = [{"source": "senapred", "item_id": "1", "reason": "AI disabled"}]
    FakeAction.raises = False

    handler = main_module._make_on_message(FakeAction, None, "fakeaction")
    handler(FakeClient(), None, FakeMessage("radiobeacon/events/item.dispatched", _dispatched_payload()))

    conn = sqlite3.connect(tmp_path / "radiobeacon.db")
    details = conn.execute(
        "SELECT details FROM audit_log WHERE event_type = 'action.fakeaction.executed'"
    ).fetchone()[0]
    assert json.loads(details)["reason"] == "AI disabled"


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


def test_on_message_processes_rearm_as_new_event_despite_same_source_item_id(tmp_path, monkeypatch):
    """A rearm (dispatcher.override.rearm_item) publishes a genuinely new
    item.dispatched CloudEvent — different event id — for the SAME
    (source, item_id) as an earlier, already-processed delivery. Keying
    idempotency on event id (not (source, item_id)) means this second,
    distinct event is correctly processed rather than silently skipped —
    the bug this fix addresses."""
    monkeypatch.setattr(main_module, "DEFAULT_DB_PATH", tmp_path / "radiobeacon.db")
    FakeAction.outputs = []
    FakeAction.raises = False
    FakeAction.run_calls = 0

    handler = main_module._make_on_message(FakeAction, None, "fakeaction")
    handler(
        FakeClient(),
        None,
        FakeMessage("radiobeacon/events/item.dispatched", _dispatched_payload(source="csn", item_id="1")),
    )
    handler(
        FakeClient(),
        None,
        FakeMessage("radiobeacon/events/item.dispatched", _dispatched_payload(source="csn", item_id="1")),
    )

    assert FakeAction.run_calls == 2  # same (source, item_id), distinct event ids — both processed


def test_already_processed_returns_false_without_event_id(tmp_path):
    conn = sqlite3.connect(tmp_path / "radiobeacon.db")
    conn.execute("CREATE TABLE audit_log (event_type TEXT, details TEXT)")

    assert main_module._already_processed(conn, "fakeaction", None) is False


def test_already_processed_true_for_matching_event_id(tmp_path):
    conn = sqlite3.connect(tmp_path / "radiobeacon.db")
    conn.execute("CREATE TABLE audit_log (event_type TEXT, details TEXT)")
    conn.execute(
        "INSERT INTO audit_log (event_type, details) VALUES (?, ?)",
        ("action.fakeaction.executed", json.dumps({"event_id": "abc-123"})),
    )

    assert main_module._already_processed(conn, "fakeaction", "abc-123") is True
    assert main_module._already_processed(conn, "fakeaction", "different-id") is False


def test_run_action_loop_uses_stable_client_id_and_clean_session_true(monkeypatch):
    """clean_session=True (not False) is deliberate: MQTT SUBSCRIBE is
    purely additive, so a persistent session across a topic rename would
    keep an old subscription alive forever alongside the new one — this
    is exactly what caused actions.chunk to double-process every dispatch
    after ACTIONS_CHUNK_SUBSCRIBE_TOPIC changed earlier this session."""
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
    assert captured["clean_session"] is True
