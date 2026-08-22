import json

import adapters.storage as storage_module
import paho.mqtt.client
import pytest

import dispatcher.mq_publisher as mq_publisher
from dispatcher.mq_publisher import publish_cloud_event


@pytest.fixture(autouse=True)
def _isolated_default_db_path(tmp_path, monkeypatch):
    """get_setting() (called by every DISPATCHER_MQ_* lookup below) opens
    its own connection against adapters.storage.DEFAULT_DB_PATH whenever a
    caller doesn't pass conn/db_path explicitly — as every call in
    mq_publisher.py does. Without this, every test here would silently
    read/create the real repo's storage/radiobeacon.db instead of a
    throwaway one."""
    monkeypatch.setattr(storage_module, "DEFAULT_DB_PATH", tmp_path / "radiobeacon.db")


class FakeMessageInfo:
    def wait_for_publish(self, timeout=None):
        pass


class FakeMqttClient:
    def __init__(self, callback_api_version=None):
        self.callback_api_version = callback_api_version
        self.connected = None
        self.loop_started = False
        self.published = []

    def connect(self, host, port):
        self.connected = (host, port)

    def loop_start(self):
        self.loop_started = True

    def publish(self, topic, payload=None, qos=0):
        self.published.append((topic, payload, qos))
        return FakeMessageInfo()


def _install_fake_client(monkeypatch) -> FakeMqttClient:
    monkeypatch.setattr(mq_publisher, "_client", None)
    fake = FakeMqttClient()
    monkeypatch.setattr(paho.mqtt.client, "Client", lambda *a, **k: fake)
    return fake


def _raise_if_called(*args, **kwargs):
    raise AssertionError("paho.mqtt.client.Client should not have been constructed")


def test_unpublished_event_type_is_a_no_op(monkeypatch):
    monkeypatch.setattr(mq_publisher, "_client", None)
    monkeypatch.setattr(paho.mqtt.client, "Client", _raise_if_called)
    monkeypatch.setenv("DISPATCHER_MQ_HOST", "localhost")

    publish_cloud_event(
        event_type="item.stored",
        actor="adapters.storage",
        source="senapred",
        item_id="1",
        details=None,
    )


def test_disabled_by_default_when_host_unset_is_a_no_op(monkeypatch):
    monkeypatch.setattr(mq_publisher, "_client", None)
    monkeypatch.setattr(paho.mqtt.client, "Client", _raise_if_called)
    monkeypatch.delenv("DISPATCHER_MQ_HOST", raising=False)

    publish_cloud_event(
        event_type="item.dispatched",
        actor="log_handler",
        source="senapred",
        item_id="1",
        details={"consumer": "log", "times_triggered": 1},
    )


def test_publish_skips_item_dispatched_redelivery(monkeypatch):
    fake = _install_fake_client(monkeypatch)
    monkeypatch.setenv("DISPATCHER_MQ_HOST", "localhost")

    publish_cloud_event(
        event_type="item.dispatched",
        actor="log_handler",
        source="senapred",
        item_id="1",
        details={"consumer": "log", "times_triggered": 2},
    )

    assert fake.published == []
    assert fake.connected is None  # a filtered event never even connects


def test_publish_publishes_item_dispatched_first_delivery(monkeypatch):
    fake = _install_fake_client(monkeypatch)
    monkeypatch.setenv("DISPATCHER_MQ_HOST", "localhost")

    publish_cloud_event(
        event_type="item.dispatched",
        actor="log_handler",
        source="senapred",
        item_id="1",
        details={"consumer": "log", "times_triggered": 1},
    )

    assert len(fake.published) == 1


def test_publish_does_not_filter_dispatch_failed(monkeypatch):
    fake = _install_fake_client(monkeypatch)
    monkeypatch.setenv("DISPATCHER_MQ_HOST", "localhost")

    publish_cloud_event(
        event_type="item.dispatch_failed",
        actor="log_handler",
        source="senapred",
        item_id="1",
        details={"consumer": "log", "error": "boom"},  # no times_triggered key at all
    )

    assert len(fake.published) == 1


def test_publish_uses_settings_row_over_env_var_for_host(monkeypatch):
    from adapters.storage import get_connection, set_setting

    fake = _install_fake_client(monkeypatch)
    monkeypatch.setenv("DISPATCHER_MQ_HOST", "env-host")
    conn = get_connection(storage_module.DEFAULT_DB_PATH)
    set_setting(conn, "DISPATCHER_MQ_HOST", "db-host")

    publish_cloud_event(
        event_type="item.dispatched",
        actor="log_handler",
        source="senapred",
        item_id="1",
        details={"consumer": "log", "times_triggered": 1},
    )

    assert fake.connected == ("db-host", 1883)


def test_publish_builds_cloud_event_envelope_with_expected_fields(monkeypatch):
    fake = _install_fake_client(monkeypatch)
    monkeypatch.setenv("DISPATCHER_MQ_HOST", "localhost")

    publish_cloud_event(
        event_type="item.dispatched",
        actor="log_handler",
        source="senapred",
        item_id="1",
        details={"consumer": "log", "dispatch_policy": "urgent", "times_triggered": 1},
    )

    assert len(fake.published) == 1
    topic, payload, qos = fake.published[0]
    assert topic == "radiobeacon/events/item.dispatched"
    assert fake.connected == ("localhost", 1883)
    assert qos == 1

    body = json.loads(payload)
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
    fake = _install_fake_client(monkeypatch)
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

    assert fake.connected == ("localhost", 18830)
    _, _, qos = fake.published[0]
    assert qos == 0


def test_publish_falls_back_to_default_port_and_qos_when_unset(monkeypatch):
    fake = _install_fake_client(monkeypatch)
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

    assert fake.connected == ("localhost", 1883)
    _, _, qos = fake.published[0]
    assert qos == 1


def test_broker_error_is_swallowed_not_raised(monkeypatch):
    monkeypatch.setattr(mq_publisher, "_client", None)

    def _raise_connection_refused(*args, **kwargs):
        raise ConnectionRefusedError("no broker listening")

    monkeypatch.setattr(paho.mqtt.client, "Client", _raise_connection_refused)
    monkeypatch.setenv("DISPATCHER_MQ_HOST", "localhost")

    publish_cloud_event(
        event_type="item.rearmed",
        actor="dispatcher.override",
        source="senapred",
        item_id="1",
        details={"consumer": "log"},
    )  # must not raise


def test_broker_error_resets_cached_client_for_next_call(monkeypatch):
    monkeypatch.setattr(mq_publisher, "_client", None)

    def _raise_connection_refused(*args, **kwargs):
        raise ConnectionRefusedError("no broker listening")

    monkeypatch.setattr(paho.mqtt.client, "Client", _raise_connection_refused)
    monkeypatch.setenv("DISPATCHER_MQ_HOST", "localhost")

    publish_cloud_event(
        event_type="item.rearmed", actor="x", source="s", item_id="1", details=None
    )

    assert mq_publisher._client is None


def test_details_none_still_produces_a_details_key_in_data(monkeypatch):
    fake = _install_fake_client(monkeypatch)
    monkeypatch.setenv("DISPATCHER_MQ_HOST", "localhost")

    publish_cloud_event(
        event_type="item.policy_overridden",
        actor="dispatcher.override",
        source="senapred",
        item_id="1",
        details=None,
    )

    _, payload, _ = fake.published[0]
    body = json.loads(payload)
    assert body["data"]["details"] == {}


def test_publish_cloud_event_does_not_escape_non_ascii_characters(monkeypatch):
    fake = _install_fake_client(monkeypatch)
    monkeypatch.setenv("DISPATCHER_MQ_HOST", "localhost")

    publish_cloud_event(
        event_type="item.rearmed",
        actor="dispatcher.override",
        source="senapred",
        item_id="1",
        details={"title": "Prevención de ñanduú"},
    )

    _, payload, _ = fake.published[0]
    assert "\\u" not in payload
    assert "Prevención de ñanduú" in payload


def test_publish_waits_for_publish_confirmation_before_returning(monkeypatch):
    monkeypatch.setattr(mq_publisher, "_client", None)
    waited = []

    class SlowConfirmMessageInfo:
        def wait_for_publish(self, timeout=None):
            waited.append(timeout)

    class ClientRequiringConfirmation(FakeMqttClient):
        def publish(self, topic, payload=None, qos=0):
            self.published.append((topic, payload, qos))
            return SlowConfirmMessageInfo()

    fake = ClientRequiringConfirmation()
    monkeypatch.setattr(paho.mqtt.client, "Client", lambda *a, **k: fake)
    monkeypatch.setenv("DISPATCHER_MQ_HOST", "localhost")
    monkeypatch.setenv("DISPATCHER_MQ_CONNECT_TIMEOUT_SECONDS", "7")

    publish_cloud_event(
        event_type="item.rearmed", actor="x", source="s", item_id="1", details=None
    )

    assert waited == [7]


def test_publish_never_confirmed_is_swallowed_not_raised(monkeypatch):
    monkeypatch.setattr(mq_publisher, "_client", None)

    class NeverPublishedMessageInfo:
        def wait_for_publish(self, timeout=None):
            raise RuntimeError("message was not published")

    class ClientThatNeverConfirms(FakeMqttClient):
        def publish(self, topic, payload=None, qos=0):
            return NeverPublishedMessageInfo()

    fake = ClientThatNeverConfirms()
    monkeypatch.setattr(paho.mqtt.client, "Client", lambda *a, **k: fake)
    monkeypatch.setenv("DISPATCHER_MQ_HOST", "localhost")

    publish_cloud_event(
        event_type="item.rearmed", actor="x", source="s", item_id="1", details=None
    )  # must not raise
