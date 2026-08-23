import json
import uuid

from adapters.storage import get_connection, record_audit_event, set_setting

import beacon.__main__ as main_module
from beacon.content import QueuedFrame, QueuedVoice
from beacon.queues import BoundedDropOldestQueue
from beacon.schedule import Slot, WindowConfig


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
    return json.dumps(
        {
            "specversion": "1.0",
            "type": "cl.radiobeacon.item.dispatched",
            "source": "radiobeacon/log_handler",
            "id": event_id or str(uuid.uuid4()),
            "data": {"source": source, "item_id": item_id},
        }
    ).encode("utf-8")


def _chunked_payload(source="senapred", item_id="1", event_id=None):
    return json.dumps(
        {
            "specversion": "1.0",
            "type": "cl.radiobeacon.item.chunked",
            "source": "radiobeacon/actions.chunk",
            "id": event_id or str(uuid.uuid4()),
            "data": {"source": source, "item_id": item_id, "chunk_count": 1},
        }
    ).encode("utf-8")


def _insert_chunk(conn, source, item_id, chunk_index=0, text="chunk text", chunk_count=1):
    conn.execute(
        "INSERT INTO chunks (source, item_id, chunk_index, chunk_count, text) VALUES (?, ?, ?, ?, ?)",
        (source, item_id, chunk_index, chunk_count, text),
    )
    conn.commit()


def _insert_item(conn, source, item_id, *, extracted_contents="raw", summary=None):
    conn.execute(
        "INSERT INTO items (source, item_id, extracted_contents, summary, fetched_at, rawdata) "
        "VALUES (?, ?, ?, ?, datetime('now'), '{}')",
        (source, item_id, extracted_contents, summary),
    )
    conn.commit()


FRAME_TOPIC = "radiobeacon/events/item.chunked"
VOICE_TOPIC = "radiobeacon/events/item.dispatched"


# --- idempotency ---


def test_already_enqueued_false_without_event_id(tmp_path):
    conn = get_connection(tmp_path / "radiobeacon.db")

    assert main_module._already_enqueued(conn, "voice", None) is False


def test_already_enqueued_true_for_matching_event_id(tmp_path):
    conn = get_connection(tmp_path / "radiobeacon.db")
    record_audit_event(
        conn, event_type="beacon.voice.enqueued", actor="beacon", details={"event_id": "abc"}
    )

    assert main_module._already_enqueued(conn, "voice", "abc") is True
    assert main_module._already_enqueued(conn, "voice", "different") is False
    assert main_module._already_enqueued(conn, "frame", "abc") is False  # scoped per kind


# --- frame enqueue (from item.chunked) ---


def test_handle_frame_event_enqueues_one_reference_per_chunk(tmp_path):
    conn = get_connection(tmp_path / "radiobeacon.db")
    _insert_chunk(conn, "csn", "1", 0, "chunk zero", chunk_count=3)
    _insert_chunk(conn, "csn", "1", 1, "chunk one", chunk_count=3)
    _insert_chunk(conn, "csn", "1", 2, "chunk two", chunk_count=3)
    frame_queue = BoundedDropOldestQueue(10)

    main_module._handle_frame_event(conn, frame_queue, "csn", "1", "event-1")

    assert frame_queue.stats().size == 3
    first = frame_queue.get_nowait()
    assert first == QueuedFrame(source="csn", item_id="1", chunk_index=0)


def test_handle_frame_event_skips_when_no_chunks_yet(tmp_path):
    conn = get_connection(tmp_path / "radiobeacon.db")
    frame_queue = BoundedDropOldestQueue(10)

    main_module._handle_frame_event(conn, frame_queue, "csn", "1", "event-1")

    assert frame_queue.stats().size == 0


def test_handle_frame_event_records_audit_event_with_event_id(tmp_path):
    conn = get_connection(tmp_path / "radiobeacon.db")
    _insert_chunk(conn, "csn", "1", 0, "chunk zero")
    frame_queue = BoundedDropOldestQueue(10)

    main_module._handle_frame_event(conn, frame_queue, "csn", "1", "event-1")

    row = conn.execute(
        "SELECT details FROM audit_log WHERE event_type = 'beacon.frame.enqueued'"
    ).fetchone()
    assert json.loads(row[0])["event_id"] == "event-1"


# --- voice enqueue (from item.dispatched) ---


def test_handle_voice_event_enqueues_reference(tmp_path):
    conn = get_connection(tmp_path / "radiobeacon.db")
    voice_queue = BoundedDropOldestQueue(10)

    main_module._handle_voice_event(conn, voice_queue, "csn", "1", "event-1")

    assert voice_queue.get_nowait() == QueuedVoice(source="csn", item_id="1")


def test_handle_voice_event_idempotent_on_same_event_id(tmp_path):
    conn = get_connection(tmp_path / "radiobeacon.db")
    voice_queue = BoundedDropOldestQueue(10)

    main_module._handle_voice_event(conn, voice_queue, "csn", "1", "event-1")
    main_module._handle_voice_event(conn, voice_queue, "csn", "1", "event-1")  # redelivered duplicate

    assert voice_queue.stats().size == 1


def test_handle_voice_event_rearm_with_new_event_id_enqueues_again(tmp_path):
    """A rearm publishes a genuinely new event id for the same
    (source, item_id) -- must be enqueued again, not skipped."""
    conn = get_connection(tmp_path / "radiobeacon.db")
    voice_queue = BoundedDropOldestQueue(10)

    main_module._handle_voice_event(conn, voice_queue, "csn", "1", "event-1")
    main_module._handle_voice_event(conn, voice_queue, "csn", "1", "event-2")

    assert voice_queue.stats().size == 2


# --- on_message routing ---


def test_on_message_routes_frame_topic_to_frame_queue(tmp_path, monkeypatch):
    monkeypatch.setattr(main_module, "DEFAULT_DB_PATH", tmp_path / "radiobeacon.db")
    conn = get_connection(tmp_path / "radiobeacon.db")
    _insert_chunk(conn, "senapred", "1", 0, "chunk zero")

    frame_queue = BoundedDropOldestQueue(10)
    voice_queue = BoundedDropOldestQueue(10)
    handler = main_module._make_on_message(FRAME_TOPIC, VOICE_TOPIC, frame_queue, voice_queue)

    handler(FakeClient(), None, FakeMessage(FRAME_TOPIC, _chunked_payload()))

    assert frame_queue.stats().size == 1
    assert voice_queue.stats().size == 0


def test_on_message_routes_voice_topic_to_voice_queue(tmp_path, monkeypatch):
    monkeypatch.setattr(main_module, "DEFAULT_DB_PATH", tmp_path / "radiobeacon.db")

    frame_queue = BoundedDropOldestQueue(10)
    voice_queue = BoundedDropOldestQueue(10)
    handler = main_module._make_on_message(FRAME_TOPIC, VOICE_TOPIC, frame_queue, voice_queue)

    handler(FakeClient(), None, FakeMessage(VOICE_TOPIC, _dispatched_payload()))

    assert voice_queue.stats().size == 1
    assert frame_queue.stats().size == 0


def test_on_message_swallows_malformed_payload(tmp_path, monkeypatch):
    monkeypatch.setattr(main_module, "DEFAULT_DB_PATH", tmp_path / "radiobeacon.db")
    frame_queue = BoundedDropOldestQueue(10)
    voice_queue = BoundedDropOldestQueue(10)
    handler = main_module._make_on_message(FRAME_TOPIC, VOICE_TOPIC, frame_queue, voice_queue)

    handler(FakeClient(), None, FakeMessage(VOICE_TOPIC, b"not json"))  # must not raise


def test_on_message_skips_event_missing_source_or_item_id(tmp_path, monkeypatch):
    monkeypatch.setattr(main_module, "DEFAULT_DB_PATH", tmp_path / "radiobeacon.db")
    frame_queue = BoundedDropOldestQueue(10)
    voice_queue = BoundedDropOldestQueue(10)
    handler = main_module._make_on_message(FRAME_TOPIC, VOICE_TOPIC, frame_queue, voice_queue)

    payload = json.dumps(
        {"specversion": "1.0", "type": "x", "source": "s", "id": "1", "data": {}}
    ).encode("utf-8")
    handler(FakeClient(), None, FakeMessage(VOICE_TOPIC, payload))

    assert voice_queue.stats().size == 0


# --- window config ---


def test_load_window_config_returns_config_from_settings(tmp_path):
    conn = get_connection(tmp_path / "radiobeacon.db")
    set_setting(conn, "BEACON_WINDOW_TOTAL_SECONDS", "90")
    set_setting(conn, "BEACON_WINDOW_VOICE_SECONDS", "60")
    set_setting(conn, "BEACON_WINDOW_FRAME_SECONDS", "30")

    config = main_module._load_window_config(conn)

    assert config == WindowConfig(total_seconds=90, voice_seconds=60, frame_seconds=30, guard_seconds=0)


def test_load_window_config_returns_none_on_invalid_combination(tmp_path):
    conn = get_connection(tmp_path / "radiobeacon.db")
    set_setting(conn, "BEACON_WINDOW_TOTAL_SECONDS", "90")
    set_setting(conn, "BEACON_WINDOW_VOICE_SECONDS", "80")
    set_setting(conn, "BEACON_WINDOW_FRAME_SECONDS", "80")  # voice+frame > total

    assert main_module._load_window_config(conn) is None


# --- _seconds_until_slot_start ---


def test_seconds_until_voice_start_from_within_voice_is_next_cycle():
    config = WindowConfig(total_seconds=90, voice_seconds=60, frame_seconds=30)
    eta = main_module._seconds_until_slot_start(config, Slot.VOICE, now=30.0)
    assert eta == 60.0  # 90 - 30 (until next cycle's voice)


def test_seconds_until_frame_start_from_within_voice():
    config = WindowConfig(total_seconds=90, voice_seconds=60, frame_seconds=30)
    eta = main_module._seconds_until_slot_start(config, Slot.FRAME, now=55.0)
    assert eta == 5.0


def test_seconds_until_frame_start_after_frame_already_started():
    config = WindowConfig(total_seconds=90, voice_seconds=60, frame_seconds=30)
    eta = main_module._seconds_until_slot_start(config, Slot.FRAME, now=70.0)
    assert eta == 90.0 - 70.0 + 60.0  # waits for next cycle's frame


def test_seconds_until_voice_start_with_zero_guard_still_correct():
    # No GUARD slot ever occurs -- eta to FRAME must anchor to frame's own
    # start (voice_seconds), not to "leaving voice."
    config = WindowConfig(total_seconds=90, voice_seconds=60, frame_seconds=30, guard_seconds=0)
    eta = main_module._seconds_until_slot_start(config, Slot.FRAME, now=58.0)
    assert eta == 2.0


# --- _maybe_control_services ---


class _RecordingServiceController:
    def __init__(self):
        self.calls = []

    def start(self, service_name):
        self.calls.append(("start", service_name))
        return True

    def stop(self, service_name):
        self.calls.append(("stop", service_name))
        return True

    def is_active(self, service_name):
        return True


def test_maybe_control_services_preps_voice_when_content_queued_and_within_lead_time(tmp_path):
    conn = get_connection(tmp_path / "radiobeacon.db")
    config = WindowConfig(total_seconds=90, voice_seconds=60, frame_seconds=30)
    voice_queue = BoundedDropOldestQueue(10)
    voice_queue.put(QueuedVoice(source="csn", item_id="1"))
    frame_queue = BoundedDropOldestQueue(10)
    controller = _RecordingServiceController()

    # now=89 -> voice starts in 1s (eta=1), lead_time=2 -> should trigger
    prepped_voice, prepped_frame = main_module._maybe_control_services(
        conn, config, 89.0, voice_queue, frame_queue, controller, "svxlink", "direwolf",
        lead_time=2.0, prepped_voice=False, prepped_frame=False,
    )

    assert prepped_voice is True
    assert ("stop", "direwolf") in controller.calls
    assert ("start", "svxlink") in controller.calls


def test_maybe_control_services_does_not_prep_when_queue_empty(tmp_path):
    conn = get_connection(tmp_path / "radiobeacon.db")
    config = WindowConfig(total_seconds=90, voice_seconds=60, frame_seconds=30)
    voice_queue = BoundedDropOldestQueue(10)  # empty
    frame_queue = BoundedDropOldestQueue(10)
    controller = _RecordingServiceController()

    prepped_voice, _ = main_module._maybe_control_services(
        conn, config, 89.0, voice_queue, frame_queue, controller, "svxlink", "direwolf",
        lead_time=2.0, prepped_voice=False, prepped_frame=False,
    )

    assert prepped_voice is False
    assert controller.calls == []


def test_maybe_control_services_does_not_double_trigger_within_same_window(tmp_path):
    conn = get_connection(tmp_path / "radiobeacon.db")
    config = WindowConfig(total_seconds=90, voice_seconds=60, frame_seconds=30)
    voice_queue = BoundedDropOldestQueue(10)
    voice_queue.put(QueuedVoice(source="csn", item_id="1"))
    frame_queue = BoundedDropOldestQueue(10)
    controller = _RecordingServiceController()

    prepped_voice, _ = main_module._maybe_control_services(
        conn, config, 89.0, voice_queue, frame_queue, controller, "svxlink", "direwolf",
        lead_time=2.0, prepped_voice=True, prepped_frame=False,  # already prepped
    )

    assert prepped_voice is True
    assert controller.calls == []  # no repeat trigger


def test_maybe_control_services_resets_flag_outside_lead_time_window(tmp_path):
    conn = get_connection(tmp_path / "radiobeacon.db")
    config = WindowConfig(total_seconds=90, voice_seconds=60, frame_seconds=30)
    voice_queue = BoundedDropOldestQueue(10)
    frame_queue = BoundedDropOldestQueue(10)
    controller = _RecordingServiceController()

    # now=30 -> eta to voice is 60s, well outside lead_time=2 -> reset.
    # No content queued either, so _switch_service (and its conn use)
    # never actually gets invoked here regardless.
    prepped_voice, _ = main_module._maybe_control_services(
        conn, config, 30.0, voice_queue, frame_queue, controller, "svxlink", "direwolf",
        lead_time=2.0, prepped_voice=True, prepped_frame=False,
    )

    assert prepped_voice is False


# --- transmit ---


def test_try_transmit_voice_skips_when_queue_empty(tmp_path):
    conn = get_connection(tmp_path / "radiobeacon.db")
    voice_queue = BoundedDropOldestQueue(10)

    main_module._try_transmit_voice(
        conn, voice_queue, _StubVoiceTransmitter(), "CD3DXZ-1", "{callsign}. {text}", 200,
        str(tmp_path), "es",
    )  # no exception, nothing to assert beyond "doesn't crash"


class _StubVoiceTransmitter:
    def __init__(self):
        self.calls = []

    def transmit(self, *, text, wav_path):
        self.calls.append(text)
        return True


def test_try_transmit_voice_skips_when_no_callsign(tmp_path):
    conn = get_connection(tmp_path / "radiobeacon.db")
    _insert_item(conn, "csn", "1", extracted_contents="raw contents")
    voice_queue = BoundedDropOldestQueue(10)
    voice_queue.put(QueuedVoice(source="csn", item_id="1"))
    transmitter = _StubVoiceTransmitter()

    main_module._try_transmit_voice(
        conn, voice_queue, transmitter, None, "{callsign}. {text}", 200, str(tmp_path), "es"
    )

    assert transmitter.calls == []
    row = conn.execute(
        "SELECT 1 FROM audit_log WHERE event_type = 'beacon.voice.skipped_no_callsign'"
    ).fetchone()
    assert row is not None


def test_try_transmit_voice_uses_resolved_content_and_transmits(tmp_path, monkeypatch):
    monkeypatch.setattr("beacon.voice.synthesize_speech", lambda *a, **k: True)
    conn = get_connection(tmp_path / "radiobeacon.db")
    _insert_item(conn, "csn", "1", extracted_contents="Sismo de magnitud 4.2.")
    voice_queue = BoundedDropOldestQueue(10)
    voice_queue.put(QueuedVoice(source="csn", item_id="1"))
    transmitter = _StubVoiceTransmitter()

    main_module._try_transmit_voice(
        conn, voice_queue, transmitter, "CD3DXZ-1", "{callsign}. {text}", 200, str(tmp_path), "es"
    )

    assert transmitter.calls == ["CD3DXZ-1. Sismo de magnitud 4.2."]
    row = conn.execute(
        "SELECT 1 FROM audit_log WHERE event_type = 'beacon.voice.transmitted'"
    ).fetchone()
    assert row is not None


class _StubKissClient:
    def __init__(self, result=True):
        self.result = result
        self.calls = []

    def send_ui_frame(self, *, source_callsign, dest_callsign, info):
        self.calls.append((source_callsign, dest_callsign, info))
        return self.result


def test_try_transmit_frame_skips_when_no_callsign(tmp_path):
    conn = get_connection(tmp_path / "radiobeacon.db")
    _insert_chunk(conn, "csn", "1", 0, "chunk text")
    frame_queue = BoundedDropOldestQueue(10)
    frame_queue.put(QueuedFrame(source="csn", item_id="1", chunk_index=0))
    kiss_client = _StubKissClient()

    main_module._try_transmit_frame(conn, frame_queue, kiss_client, None, "WXALRT")

    assert kiss_client.calls == []


def test_try_transmit_frame_sends_and_records_audit_event(tmp_path):
    conn = get_connection(tmp_path / "radiobeacon.db")
    _insert_chunk(conn, "csn", "1", 0, "chunk text")
    frame_queue = BoundedDropOldestQueue(10)
    frame_queue.put(QueuedFrame(source="csn", item_id="1", chunk_index=0))
    kiss_client = _StubKissClient()

    main_module._try_transmit_frame(conn, frame_queue, kiss_client, "CD3DXZ-1", "WXALRT")

    assert kiss_client.calls == [("CD3DXZ-1", "WXALRT", b"chunk text")]
    row = conn.execute(
        "SELECT 1 FROM audit_log WHERE event_type = 'beacon.frame.transmitted'"
    ).fetchone()
    assert row is not None


def test_try_transmit_frame_records_dropped_too_long(tmp_path):
    conn = get_connection(tmp_path / "radiobeacon.db")
    _insert_chunk(conn, "csn", "1", 0, "x" * 300)  # over the AX.25 hard limit
    frame_queue = BoundedDropOldestQueue(10)
    frame_queue.put(QueuedFrame(source="csn", item_id="1", chunk_index=0))
    kiss_client = _StubKissClient()

    main_module._try_transmit_frame(conn, frame_queue, kiss_client, "CD3DXZ-1", "WXALRT")

    assert kiss_client.calls == []
    row = conn.execute(
        "SELECT 1 FROM audit_log WHERE event_type = 'beacon.frame.dropped_too_long'"
    ).fetchone()
    assert row is not None


def test_try_transmit_frame_records_transmit_failed_on_send_failure(tmp_path):
    conn = get_connection(tmp_path / "radiobeacon.db")
    _insert_chunk(conn, "csn", "1", 0, "chunk text")
    frame_queue = BoundedDropOldestQueue(10)
    frame_queue.put(QueuedFrame(source="csn", item_id="1", chunk_index=0))
    kiss_client = _StubKissClient(result=False)

    main_module._try_transmit_frame(conn, frame_queue, kiss_client, "CD3DXZ-1", "WXALRT")

    row = conn.execute(
        "SELECT 1 FROM audit_log WHERE event_type = 'beacon.frame.transmit_failed'"
    ).fetchone()
    assert row is not None


# --- factories ---


def test_build_voice_transmitter_defaults_to_logging(tmp_path):
    conn = get_connection(tmp_path / "radiobeacon.db")

    transmitter = main_module._build_voice_transmitter(conn)

    assert type(transmitter).__name__ == "LoggingVoiceTransmitter"


def test_build_service_controller_defaults_to_logging(tmp_path):
    conn = get_connection(tmp_path / "radiobeacon.db")

    controller = main_module._build_service_controller(conn)

    assert type(controller).__name__ == "LoggingServiceController"


def test_build_service_controller_systemctl_when_configured(tmp_path):
    conn = get_connection(tmp_path / "radiobeacon.db")
    set_setting(conn, "BEACON_SERVICE_CONTROLLER", "systemctl")

    controller = main_module._build_service_controller(conn)

    assert type(controller).__name__ == "SystemctlServiceController"
