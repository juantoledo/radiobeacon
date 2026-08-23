import json
import uuid
from datetime import datetime, timezone

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


def _content_ready_payload(source="senapred", item_id="1", event_id=None):
    return json.dumps(
        {
            "specversion": "1.0",
            "type": "cl.radiobeacon.item.content_ready",
            "source": "radiobeacon/actions.content_ready",
            "id": event_id or str(uuid.uuid4()),
            "data": {"source": source, "item_id": item_id},
        }
    ).encode("utf-8")


def _insert_chunk(conn, source, item_id, chunk_index=0, text="chunk text", chunk_count=1):
    conn.execute(
        "INSERT INTO chunks (source, item_id, chunk_index, chunk_count, text) VALUES (?, ?, ?, ?, ?)",
        (source, item_id, chunk_index, chunk_count, text),
    )
    conn.commit()


def _insert_item(conn, source, item_id, *, extracted_contents="raw", summary=None, source_date_time=None):
    conn.execute(
        "INSERT INTO items (source, item_id, extracted_contents, summary, source_date_time, fetched_at, rawdata) "
        "VALUES (?, ?, ?, ?, ?, datetime('now'), '{}')",
        (source, item_id, extracted_contents, summary, source_date_time),
    )
    conn.commit()


CONTENT_READY_TOPIC = "radiobeacon/events/item.content_ready"


# --- idempotency ---


def test_already_enqueued_false_without_event_id(tmp_path):
    conn = get_connection(tmp_path / "radiobeacon.db")

    assert main_module._already_enqueued(conn, None) is False


def test_already_enqueued_true_for_matching_event_id(tmp_path):
    conn = get_connection(tmp_path / "radiobeacon.db")
    record_audit_event(
        conn, event_type="beacon.content_ready.enqueued", actor="beacon", details={"event_id": "abc"}
    )

    assert main_module._already_enqueued(conn, "abc") is True
    assert main_module._already_enqueued(conn, "different") is False


# --- content_ready enqueue ---


def test_handle_content_ready_event_enqueues_one_frame_per_chunk(tmp_path):
    conn = get_connection(tmp_path / "radiobeacon.db")
    _insert_chunk(conn, "csn", "1", 0, "chunk zero", chunk_count=3)
    _insert_chunk(conn, "csn", "1", 1, "chunk one", chunk_count=3)
    _insert_chunk(conn, "csn", "1", 2, "chunk two", chunk_count=3)
    frame_queue = BoundedDropOldestQueue(10)
    voice_queue = BoundedDropOldestQueue(10)

    main_module._handle_content_ready_event(conn, frame_queue, voice_queue, "csn", "1", "event-1")

    assert frame_queue.stats().size == 3
    assert frame_queue.get_nowait() == QueuedFrame(source="csn", item_id="1", chunk_index=0)
    assert voice_queue.stats().size == 1


def test_handle_content_ready_event_no_chunks_yet_enqueues_no_frame(tmp_path):
    conn = get_connection(tmp_path / "radiobeacon.db")
    frame_queue = BoundedDropOldestQueue(10)
    voice_queue = BoundedDropOldestQueue(10)

    main_module._handle_content_ready_event(conn, frame_queue, voice_queue, "csn", "1", "event-1")

    assert frame_queue.stats().size == 0
    assert voice_queue.stats().size == 1


def test_handle_content_ready_event_idempotent_on_same_event_id(tmp_path):
    conn = get_connection(tmp_path / "radiobeacon.db")
    _insert_chunk(conn, "csn", "1", 0, "chunk zero")
    frame_queue = BoundedDropOldestQueue(10)
    voice_queue = BoundedDropOldestQueue(10)

    main_module._handle_content_ready_event(conn, frame_queue, voice_queue, "csn", "1", "event-1")
    main_module._handle_content_ready_event(conn, frame_queue, voice_queue, "csn", "1", "event-1")

    assert frame_queue.stats().size == 1
    assert voice_queue.stats().size == 1


def test_handle_content_ready_event_rearm_with_new_event_id_enqueues_again(tmp_path):
    conn = get_connection(tmp_path / "radiobeacon.db")
    _insert_chunk(conn, "csn", "1", 0, "chunk zero")
    frame_queue = BoundedDropOldestQueue(10)
    voice_queue = BoundedDropOldestQueue(10)

    main_module._handle_content_ready_event(conn, frame_queue, voice_queue, "csn", "1", "event-1")
    main_module._handle_content_ready_event(conn, frame_queue, voice_queue, "csn", "1", "event-2")

    assert frame_queue.stats().size == 2
    assert voice_queue.stats().size == 2


def test_handle_content_ready_event_records_audit_event_with_event_id(tmp_path):
    conn = get_connection(tmp_path / "radiobeacon.db")
    _insert_chunk(conn, "csn", "1", 0, "chunk zero")
    frame_queue = BoundedDropOldestQueue(10)
    voice_queue = BoundedDropOldestQueue(10)

    main_module._handle_content_ready_event(conn, frame_queue, voice_queue, "csn", "1", "event-1")

    row = conn.execute(
        "SELECT details FROM audit_log WHERE event_type = 'beacon.content_ready.enqueued'"
    ).fetchone()
    details = json.loads(row[0])
    assert details["event_id"] == "event-1"
    assert details["frame_count"] == 1


# --- on_message routing ---


def test_on_message_routes_content_ready_to_both_queues(tmp_path, monkeypatch):
    monkeypatch.setattr(main_module, "DEFAULT_DB_PATH", tmp_path / "radiobeacon.db")
    conn = get_connection(tmp_path / "radiobeacon.db")
    _insert_chunk(conn, "senapred", "1", 0, "chunk zero")

    frame_queue = BoundedDropOldestQueue(10)
    voice_queue = BoundedDropOldestQueue(10)
    handler = main_module._make_on_message(CONTENT_READY_TOPIC, frame_queue, voice_queue)

    handler(FakeClient(), None, FakeMessage(CONTENT_READY_TOPIC, _content_ready_payload()))

    assert frame_queue.stats().size == 1
    assert voice_queue.stats().size == 1


def test_on_message_swallows_malformed_payload(tmp_path, monkeypatch):
    monkeypatch.setattr(main_module, "DEFAULT_DB_PATH", tmp_path / "radiobeacon.db")
    frame_queue = BoundedDropOldestQueue(10)
    voice_queue = BoundedDropOldestQueue(10)
    handler = main_module._make_on_message(CONTENT_READY_TOPIC, frame_queue, voice_queue)

    handler(FakeClient(), None, FakeMessage(CONTENT_READY_TOPIC, b"not json"))  # must not raise


def test_on_message_skips_event_missing_source_or_item_id(tmp_path, monkeypatch):
    monkeypatch.setattr(main_module, "DEFAULT_DB_PATH", tmp_path / "radiobeacon.db")
    frame_queue = BoundedDropOldestQueue(10)
    voice_queue = BoundedDropOldestQueue(10)
    handler = main_module._make_on_message(CONTENT_READY_TOPIC, frame_queue, voice_queue)

    payload = json.dumps(
        {"specversion": "1.0", "type": "x", "source": "s", "id": "1", "data": {}}
    ).encode("utf-8")
    handler(FakeClient(), None, FakeMessage(CONTENT_READY_TOPIC, payload))

    assert voice_queue.stats().size == 0
    assert frame_queue.stats().size == 0


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


# --- _drain_and_transmit ---


def test_drain_and_transmit_calls_once_per_queued_item(tmp_path):
    import threading

    frame_queue = BoundedDropOldestQueue(10)
    for i in range(4):
        frame_queue.put(QueuedFrame(source="csn", item_id=str(i), chunk_index=0))

    calls = []

    def transmit_once():
        frame_queue.get_nowait()
        calls.append(1)

    attempted = main_module._drain_and_transmit(threading.Event(), frame_queue, 0.0, transmit_once)

    assert attempted == 4
    assert len(calls) == 4
    assert frame_queue.stats().size == 0


def test_drain_and_transmit_waits_between_items_not_before_first_or_after_last(tmp_path):
    import threading

    frame_queue = BoundedDropOldestQueue(10)
    for i in range(3):
        frame_queue.put(QueuedFrame(source="csn", item_id=str(i), chunk_index=0))

    waits = []

    class _RecordingStopEvent(threading.Event):
        def wait(self, timeout=None):
            waits.append(timeout)
            return False

    def transmit_once():
        frame_queue.get_nowait()

    main_module._drain_and_transmit(_RecordingStopEvent(), frame_queue, 2.5, transmit_once)

    # 3 items -> 2 inter-item waits (not before the first, not after the last)
    assert waits == [2.5, 2.5]


def test_drain_and_transmit_stops_early_when_stop_event_set(tmp_path):
    import threading

    frame_queue = BoundedDropOldestQueue(10)
    for i in range(5):
        frame_queue.put(QueuedFrame(source="csn", item_id=str(i), chunk_index=0))

    stop_event = threading.Event()
    calls = []

    def transmit_once():
        frame_queue.get_nowait()
        calls.append(1)
        if len(calls) == 2:
            stop_event.set()  # simulate shutdown mid-drain

    attempted = main_module._drain_and_transmit(stop_event, frame_queue, 0.0, transmit_once)

    assert attempted == 2  # stopped early, backlog not fully drained
    assert frame_queue.stats().size == 3


def test_drain_and_transmit_noop_on_empty_queue(tmp_path):
    import threading

    frame_queue = BoundedDropOldestQueue(10)

    attempted = main_module._drain_and_transmit(threading.Event(), frame_queue, 0.0, lambda: None)

    assert attempted == 0


# --- _format_source_date_time ---


def test_format_source_date_time_converts_and_formats(tmp_path, monkeypatch):
    monkeypatch.setenv("DISPLAY_TIMEZONE", "UTC")
    conn = get_connection(tmp_path / "radiobeacon.db")
    dt = datetime(2026, 8, 22, 14, 30, tzinfo=timezone.utc)
    _insert_item(conn, "senapred", "1", source_date_time=dt.isoformat())

    result = main_module._format_source_date_time(conn, "senapred", "1", "%d-%m-%Y %H:%M")

    assert result == "22-08-2026 14:30"


def test_format_source_date_time_blank_when_no_source_date_time(tmp_path):
    conn = get_connection(tmp_path / "radiobeacon.db")
    _insert_item(conn, "senapred", "1", source_date_time=None)

    assert main_module._format_source_date_time(conn, "senapred", "1", "%d-%m-%Y %H:%M") == ""


# --- transmit ---


def test_try_transmit_voice_skips_when_queue_empty(tmp_path):
    conn = get_connection(tmp_path / "radiobeacon.db")
    voice_queue = BoundedDropOldestQueue(10)

    main_module._try_transmit_voice(
        conn, voice_queue, _StubVoiceTransmitter(), "CD3DXZ-1", "{callsign}. {text}", 200,
        str(tmp_path), "es", "%d-%m-%Y %H:%M",
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
        conn, voice_queue, transmitter, None, "{callsign}. {text}", 200, str(tmp_path), "es", "%d-%m-%Y %H:%M",
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
        conn, voice_queue, transmitter, "CD3DXZ-1", "{callsign}. {text}", 200, str(tmp_path), "es", "%d-%m-%Y %H:%M",
    )

    assert transmitter.calls == ["CD3DXZ-1. Sismo de magnitud 4.2."]
    row = conn.execute(
        "SELECT 1 FROM audit_log WHERE event_type = 'beacon.voice.transmitted'"
    ).fetchone()
    assert row is not None


def test_try_transmit_voice_resolves_and_renders_date_end_to_end(tmp_path, monkeypatch):
    monkeypatch.setattr("beacon.voice.synthesize_speech", lambda *a, **k: True)
    monkeypatch.setenv("DISPLAY_TIMEZONE", "UTC")
    conn = get_connection(tmp_path / "radiobeacon.db")
    dt = datetime(2026, 8, 22, 14, 30, tzinfo=timezone.utc)
    _insert_item(
        conn, "csn", "1", extracted_contents="Sismo de magnitud 4.2.", source_date_time=dt.isoformat()
    )
    voice_queue = BoundedDropOldestQueue(10)
    voice_queue.put(QueuedVoice(source="csn", item_id="1"))
    transmitter = _StubVoiceTransmitter()

    main_module._try_transmit_voice(
        conn, voice_queue, transmitter, "CD3DXZ-1", "{callsign}. {text}. {date}", 200,
        str(tmp_path), "es", "%d-%m-%Y %H:%M",
    )

    assert transmitter.calls == ["CD3DXZ-1. Sismo de magnitud 4.2.. 22-08-2026 14:30"]


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


def test_try_transmit_frame_applies_prefix_and_suffix_to_actual_transmitted_bytes(tmp_path):
    conn = get_connection(tmp_path / "radiobeacon.db")
    _insert_chunk(conn, "csn", "1", 0, "chunk text")
    frame_queue = BoundedDropOldestQueue(10)
    frame_queue.put(QueuedFrame(source="csn", item_id="1", chunk_index=0))
    kiss_client = _StubKissClient()

    main_module._try_transmit_frame(
        conn, frame_queue, kiss_client, "CD3DXZ-1", "WXALRT", prefix=">> ", suffix=" [EXPERIMENTAL]"
    )

    assert kiss_client.calls == [("CD3DXZ-1", "WXALRT", b">> chunk text [EXPERIMENTAL]")]
    row = conn.execute(
        "SELECT details FROM audit_log WHERE event_type = 'beacon.frame.transmitted'"
    ).fetchone()
    assert json.loads(row[0])["tnc2"] == "CD3DXZ-1>WXALRT:>> chunk text [EXPERIMENTAL]"


def test_try_transmit_frame_resolves_and_renders_date_end_to_end(tmp_path, monkeypatch):
    monkeypatch.setenv("DISPLAY_TIMEZONE", "UTC")
    conn = get_connection(tmp_path / "radiobeacon.db")
    dt = datetime(2026, 8, 22, 14, 30, tzinfo=timezone.utc)
    _insert_item(conn, "csn", "1", source_date_time=dt.isoformat())
    _insert_chunk(conn, "csn", "1", 0, "chunk text")
    frame_queue = BoundedDropOldestQueue(10)
    frame_queue.put(QueuedFrame(source="csn", item_id="1", chunk_index=0))
    kiss_client = _StubKissClient()

    main_module._try_transmit_frame(
        conn, frame_queue, kiss_client, "CD3DXZ-1", "WXALRT",
        suffix=" {date}", date_format="%d-%m-%Y %H:%M",
    )

    assert kiss_client.calls == [("CD3DXZ-1", "WXALRT", b"chunk text 22-08-2026 14:30")]


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
