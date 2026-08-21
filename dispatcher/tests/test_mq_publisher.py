import json

import paho.mqtt.publish

from dispatcher.mq_publisher import publish_cloud_event


def _raise_if_called(*args, **kwargs):
    raise AssertionError("paho.mqtt.publish.single should not have been called")


def test_unpublished_event_type_is_a_no_op(monkeypatch):
    monkeypatch.setattr(paho.mqtt.publish, "single", _raise_if_called)
    monkeypatch.setenv("DISPATCHER_MQ_HOST", "localhost")

    publish_cloud_event(
        event_type="item.stored",
        actor="adapters.storage",
        source="senapred",
        item_id="1",
        details=None,
    )


def test_disabled_by_default_when_host_unset_is_a_no_op(monkeypatch):
    monkeypatch.setattr(paho.mqtt.publish, "single", _raise_if_called)
    monkeypatch.delenv("DISPATCHER_MQ_HOST", raising=False)

    publish_cloud_event(
        event_type="item.dispatched",
        actor="log_handler",
        source="senapred",
        item_id="1",
        details={"consumer": "log"},
    )


def test_publish_builds_cloud_event_envelope_with_expected_fields(monkeypatch):
    calls = []
    monkeypatch.setattr(
        paho.mqtt.publish, "single", lambda topic, **kwargs: calls.append((topic, kwargs))
    )
    monkeypatch.setenv("DISPATCHER_MQ_HOST", "localhost")

    publish_cloud_event(
        event_type="item.dispatched",
        actor="log_handler",
        source="senapred",
        item_id="1",
        details={"consumer": "log", "dispatch_policy": "urgent", "times_triggered": 1},
    )

    assert len(calls) == 1
    topic, kwargs = calls[0]
    assert topic == "radiobeacon/events/item.dispatched"
    assert kwargs["hostname"] == "localhost"
    assert kwargs["port"] == 1883
    assert kwargs["qos"] == 1

    body = json.loads(kwargs["payload"])
    assert body["type"] == "cl.radiobeacon.item.dispatched"
    assert body["source"] == "radiobeacon/log_handler"
    assert body["specversion"] == "1.0"
    assert body["data"] == {
        "source": "senapred",
        "item_id": "1",
        "actor": "log_handler",
        "details": {"consumer": "log", "dispatch_policy": "urgent", "times_triggered": 1},
    }


def test_publish_uses_configured_port_and_qos(monkeypatch):
    calls = []
    monkeypatch.setattr(
        paho.mqtt.publish, "single", lambda topic, **kwargs: calls.append((topic, kwargs))
    )
    monkeypatch.setenv("DISPATCHER_MQ_HOST", "localhost")
    monkeypatch.setenv("DISPATCHER_MQ_PORT", "18830")
    monkeypatch.setenv("DISPATCHER_MQ_QOS", "0")

    publish_cloud_event(
        event_type="item.discovered",
        actor="dispatcher.watcher",
        source="csn",
        item_id="2",
        details=None,
    )

    _, kwargs = calls[0]
    assert kwargs["port"] == 18830
    assert kwargs["qos"] == 0


def test_publish_falls_back_to_default_port_and_qos_when_unset(monkeypatch):
    calls = []
    monkeypatch.setattr(
        paho.mqtt.publish, "single", lambda topic, **kwargs: calls.append((topic, kwargs))
    )
    monkeypatch.setenv("DISPATCHER_MQ_HOST", "localhost")
    monkeypatch.delenv("DISPATCHER_MQ_PORT", raising=False)
    monkeypatch.delenv("DISPATCHER_MQ_QOS", raising=False)

    publish_cloud_event(
        event_type="item.discovered",
        actor="dispatcher.watcher",
        source="csn",
        item_id="2",
        details=None,
    )

    _, kwargs = calls[0]
    assert kwargs["port"] == 1883
    assert kwargs["qos"] == 1


def test_broker_error_is_swallowed_not_raised(monkeypatch):
    def _raise_connection_refused(*args, **kwargs):
        raise ConnectionRefusedError("no broker listening")

    monkeypatch.setattr(paho.mqtt.publish, "single", _raise_connection_refused)
    monkeypatch.setenv("DISPATCHER_MQ_HOST", "localhost")

    publish_cloud_event(
        event_type="item.rearmed",
        actor="dispatcher.override",
        source="senapred",
        item_id="1",
        details={"consumer": "log"},
    )  # must not raise


def test_details_none_still_produces_a_details_key_in_data(monkeypatch):
    calls = []
    monkeypatch.setattr(
        paho.mqtt.publish, "single", lambda topic, **kwargs: calls.append((topic, kwargs))
    )
    monkeypatch.setenv("DISPATCHER_MQ_HOST", "localhost")

    publish_cloud_event(
        event_type="item.policy_overridden",
        actor="dispatcher.override",
        source="senapred",
        item_id="1",
        details=None,
    )

    _, kwargs = calls[0]
    body = json.loads(kwargs["payload"])
    assert body["data"]["details"] == {}
