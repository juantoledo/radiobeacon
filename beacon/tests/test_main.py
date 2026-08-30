import json
import threading
import uuid
from datetime import datetime, timedelta, timezone

from adapters.storage import (
    add_tx_schedule_unit,
    get_connection,
    mark_item_ready_published,
    record_audit_event,
    set_setting,
)
from adapters.transmit_policy import set_policy

import beacon.__main__ as main_module
from beacon.schedule import Slot, SlotState, WindowConfig


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


def _insert_item(
    conn, source, item_id, *,
    extracted_contents="raw", summary=None, source_date_time=None,
    extracted_title=None, url=None, item_type=None, subtype=None, transmit_policy=None,
):
    conn.execute(
        "INSERT INTO items (source, item_id, extracted_contents, summary, source_date_time, "
        "extracted_title, url, type, subtype, transmit_policy, fetched_at, rawdata) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, datetime('now'), '{}')",
        (source, item_id, extracted_contents, summary, source_date_time, extracted_title,
         url, item_type, subtype, transmit_policy),
    )
    conn.commit()


def _schedule(conn, kind=None):
    sql = "SELECT source, item_id, kind, ref, transmit_policy, sent_count, last_transmitted_at FROM beacon_tx_schedule"
    params = ()
    if kind is not None:
        sql += " WHERE kind = ?"
        params = (kind,)
    return conn.execute(sql + " ORDER BY kind, ref", params).fetchall()


def _count(conn, kind):
    return conn.execute(
        "SELECT COUNT(*) FROM beacon_tx_schedule WHERE kind = ?", (kind,)
    ).fetchone()[0]


CONTENT_READY_TOPIC = "radiobeacon/events/item.content_ready"


def _ctx(**overrides):
    ctx = {
        "callsign": "CD3DXZ-1",
        "voice_template": "{callsign}. {text}",
        "voice_max_chars": 200,
        "date_format": "%d-%m-%Y %H:%M",
        "destination": "WXALRT",
        "frame_prefix": "",
        "frame_suffix": "",
        "voice_prefix": "",
        "voice_suffix": "",
        "wav_dir": "/tmp",
        "tts_voice": "es",
        "tts_engine": "espeak",
        "tts_piper_model": "",
        "tts_piper_binary": "piper",
        "voice_transmitter": None,
        "kiss_client": None,
    }
    ctx.update(overrides)
    return ctx


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


# --- content_ready -> schedule rows ---


def test_handle_content_ready_event_schedules_one_frame_row_per_chunk_and_one_voice(tmp_path):
    conn = get_connection(tmp_path / "radiobeacon.db")
    _insert_item(conn, "csn", "1", transmit_policy="urgent")
    _insert_chunk(conn, "csn", "1", 0, "chunk zero", chunk_count=3)
    _insert_chunk(conn, "csn", "1", 1, "chunk one", chunk_count=3)
    _insert_chunk(conn, "csn", "1", 2, "chunk two", chunk_count=3)

    main_module._handle_content_ready_event(conn, "csn", "1", "event-1")

    assert _count(conn, "frame") == 3
    assert _count(conn, "voice") == 1
    frame0 = conn.execute(
        "SELECT ref, transmit_policy, sent_count FROM beacon_tx_schedule "
        "WHERE kind='frame' AND ref='0'"
    ).fetchone()
    assert frame0 == ("0", "urgent", 0)


def test_handle_content_ready_event_no_chunks_yet_schedules_only_voice(tmp_path):
    conn = get_connection(tmp_path / "radiobeacon.db")
    _insert_item(conn, "csn", "1")

    main_module._handle_content_ready_event(conn, "csn", "1", "event-1")

    assert _count(conn, "frame") == 0
    assert _count(conn, "voice") == 1


def test_handle_content_ready_event_idempotent_on_same_event_id(tmp_path):
    conn = get_connection(tmp_path / "radiobeacon.db")
    _insert_item(conn, "csn", "1")
    _insert_chunk(conn, "csn", "1", 0, "chunk zero")

    main_module._handle_content_ready_event(conn, "csn", "1", "event-1")
    main_module._handle_content_ready_event(conn, "csn", "1", "event-1")

    assert _count(conn, "frame") == 1
    assert _count(conn, "voice") == 1


def test_handle_content_ready_event_rearm_with_new_event_id_resets_rows(tmp_path):
    conn = get_connection(tmp_path / "radiobeacon.db")
    _insert_item(conn, "csn", "1")
    _insert_chunk(conn, "csn", "1", 0, "chunk zero")

    main_module._handle_content_ready_event(conn, "csn", "1", "event-1")
    # simulate progress
    conn.execute("UPDATE beacon_tx_schedule SET sent_count = 3, last_transmitted_at = '2020-01-01T00:00:00'")
    conn.commit()

    main_module._handle_content_ready_event(conn, "csn", "1", "event-2")

    # PK upsert: still one row per (kind, ref), but reset
    assert _count(conn, "frame") == 1
    row = conn.execute(
        "SELECT sent_count, last_transmitted_at, enqueued_event_id FROM beacon_tx_schedule "
        "WHERE kind='frame'"
    ).fetchone()
    assert row == (0, None, "event-2")


def test_handle_content_ready_event_records_audit_event_with_event_id(tmp_path):
    conn = get_connection(tmp_path / "radiobeacon.db")
    _insert_item(conn, "csn", "1")
    _insert_chunk(conn, "csn", "1", 0, "chunk zero")

    main_module._handle_content_ready_event(conn, "csn", "1", "event-1")

    row = conn.execute(
        "SELECT details FROM audit_log WHERE event_type = 'beacon.content_ready.enqueued'"
    ).fetchone()
    details = json.loads(row[0])
    assert details["event_id"] == "event-1"
    assert details["frame_count"] == 1
    assert details["voice_enqueued"] is True


def test_handle_content_ready_event_sets_wake_event(tmp_path):
    conn = get_connection(tmp_path / "radiobeacon.db")
    _insert_item(conn, "csn", "1")
    wake_event = threading.Event()

    main_module._handle_content_ready_event(conn, "csn", "1", "event-1", wake_event)

    assert wake_event.is_set()


def test_handle_content_ready_event_wake_event_optional(tmp_path):
    conn = get_connection(tmp_path / "radiobeacon.db")
    _insert_item(conn, "csn", "1")

    main_module._handle_content_ready_event(conn, "csn", "1", "event-1")

    assert _count(conn, "voice") == 1


def test_handle_content_ready_event_does_not_set_wake_event_when_already_scheduled(tmp_path):
    conn = get_connection(tmp_path / "radiobeacon.db")
    _insert_item(conn, "csn", "1")
    main_module._handle_content_ready_event(conn, "csn", "1", "event-1")

    wake_event = threading.Event()
    main_module._handle_content_ready_event(conn, "csn", "1", "event-1", wake_event)

    assert not wake_event.is_set()


# --- _reconcile_missed_content_ready ---


def test_reconcile_schedules_item_readiness_row_never_scheduled(tmp_path):
    conn = get_connection(tmp_path / "radiobeacon.db")
    _insert_item(conn, "csn", "1")
    _insert_chunk(conn, "csn", "1", 0, "chunk zero")
    mark_item_ready_published(conn, "csn", "1")

    reconciled = main_module._reconcile_missed_content_ready(conn)

    assert reconciled == 1
    assert _count(conn, "frame") == 1
    assert _count(conn, "voice") == 1


def test_reconcile_skips_item_already_scheduled_via_live_mqtt(tmp_path):
    conn = get_connection(tmp_path / "radiobeacon.db")
    _insert_item(conn, "csn", "1")
    _insert_chunk(conn, "csn", "1", 0, "chunk zero")
    main_module._handle_content_ready_event(conn, "csn", "1", "event-1")
    mark_item_ready_published(conn, "csn", "1")

    assert main_module._reconcile_missed_content_ready(conn) == 0


def test_reconcile_is_a_noop_with_nothing_pending(tmp_path):
    conn = get_connection(tmp_path / "radiobeacon.db")

    assert main_module._reconcile_missed_content_ready(conn) == 0
    assert _count(conn, "frame") == 0


def test_reconcile_reschedules_after_a_rearm_republish(tmp_path):
    conn = get_connection(tmp_path / "radiobeacon.db")
    _insert_item(conn, "csn", "1")
    _insert_chunk(conn, "csn", "1", 0, "chunk zero")
    main_module._handle_content_ready_event(conn, "csn", "1", "event-1")
    mark_item_ready_published(conn, "csn", "1")

    conn.execute(
        "UPDATE item_readiness SET published_at = '2099-01-01 00:00:00' "
        "WHERE source = 'csn' AND item_id = '1'"
    )
    conn.commit()

    assert main_module._reconcile_missed_content_ready(conn) == 1


# --- on_message routing ---


def test_on_message_routes_content_ready_to_schedule(tmp_path, monkeypatch):
    monkeypatch.setattr(main_module, "DEFAULT_DB_PATH", tmp_path / "radiobeacon.db")
    conn = get_connection(tmp_path / "radiobeacon.db")
    _insert_item(conn, "senapred", "1")
    _insert_chunk(conn, "senapred", "1", 0, "chunk zero")

    handler = main_module._make_on_message(CONTENT_READY_TOPIC, threading.Event())
    handler(FakeClient(), None, FakeMessage(CONTENT_READY_TOPIC, _content_ready_payload()))

    assert _count(conn, "frame") == 1
    assert _count(conn, "voice") == 1


def test_on_message_swallows_malformed_payload(tmp_path, monkeypatch):
    monkeypatch.setattr(main_module, "DEFAULT_DB_PATH", tmp_path / "radiobeacon.db")
    handler = main_module._make_on_message(CONTENT_READY_TOPIC, threading.Event())

    handler(FakeClient(), None, FakeMessage(CONTENT_READY_TOPIC, b"not json"))  # must not raise


def test_on_message_skips_event_missing_source_or_item_id(tmp_path, monkeypatch):
    monkeypatch.setattr(main_module, "DEFAULT_DB_PATH", tmp_path / "radiobeacon.db")
    conn = get_connection(tmp_path / "radiobeacon.db")
    handler = main_module._make_on_message(CONTENT_READY_TOPIC, threading.Event())

    payload = json.dumps(
        {"specversion": "1.0", "type": "x", "source": "s", "id": "1", "data": {}}
    ).encode("utf-8")
    handler(FakeClient(), None, FakeMessage(CONTENT_READY_TOPIC, payload))

    assert _count(conn, "voice") == 0


# --- _write_heartbeat ---


def test_write_heartbeat_persists_slot_and_schedule_counts(tmp_path):
    conn = get_connection(tmp_path / "radiobeacon.db")
    add_tx_schedule_unit(conn, "csn", "1", "voice", "", "urgent", "e1")
    add_tx_schedule_unit(conn, "csn", "1", "frame", "0", "urgent", "e1")
    add_tx_schedule_unit(conn, "csn", "1", "frame", "1", "urgent", "e1")
    state = SlotState(
        slot=Slot.VOICE, cycle_index=3, elapsed_in_cycle=12.34, remaining_in_slot=47.66,
        cycle_started_at=1000.0,
    )

    main_module._write_heartbeat(conn, state)

    status = {
        row[0]: row[1]
        for row in conn.execute("SELECT key, value FROM beacon_status").fetchall()
    }
    assert status["current_slot"] == "voice"
    assert status["current_cycle_index"] == "3"
    assert status["current_cycle_elapsed_seconds"] == "12.3"
    assert status["voice_queue_depth"] == "1"
    assert status["frame_queue_depth"] == "2"


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
    set_setting(conn, "BEACON_WINDOW_FRAME_SECONDS", "80")

    assert main_module._load_window_config(conn) is None


# --- _seconds_until_slot_start ---


def test_seconds_until_voice_start_from_within_voice_is_next_cycle():
    config = WindowConfig(total_seconds=90, voice_seconds=60, frame_seconds=30)
    assert main_module._seconds_until_slot_start(config, Slot.VOICE, now=30.0) == 60.0


def test_seconds_until_frame_start_from_within_voice():
    config = WindowConfig(total_seconds=90, voice_seconds=60, frame_seconds=30)
    assert main_module._seconds_until_slot_start(config, Slot.FRAME, now=55.0) == 5.0


def test_seconds_until_voice_start_with_zero_guard_still_correct():
    config = WindowConfig(total_seconds=90, voice_seconds=60, frame_seconds=30, guard_seconds=0)
    assert main_module._seconds_until_slot_start(config, Slot.FRAME, now=58.0) == 2.0


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


def test_maybe_control_services_preps_voice_when_scheduled_and_within_lead_time(tmp_path):
    conn = get_connection(tmp_path / "radiobeacon.db")
    add_tx_schedule_unit(conn, "csn", "1", "voice", "", "urgent", "e1")
    config = WindowConfig(total_seconds=90, voice_seconds=60, frame_seconds=30)
    controller = _RecordingServiceController()

    prepped_voice, _ = main_module._maybe_control_services(
        conn, config, 89.0, controller, "svxlink", "direwolf",
        lead_time=2.0, prepped_voice=False, prepped_frame=False,
    )

    assert prepped_voice is True
    assert ("stop", "direwolf") in controller.calls
    assert ("start", "svxlink") in controller.calls


def test_maybe_control_services_does_not_prep_when_nothing_scheduled(tmp_path):
    conn = get_connection(tmp_path / "radiobeacon.db")
    config = WindowConfig(total_seconds=90, voice_seconds=60, frame_seconds=30)
    controller = _RecordingServiceController()

    prepped_voice, _ = main_module._maybe_control_services(
        conn, config, 89.0, controller, "svxlink", "direwolf",
        lead_time=2.0, prepped_voice=False, prepped_frame=False,
    )

    assert prepped_voice is False
    assert controller.calls == []


def test_maybe_control_services_does_not_double_trigger_within_same_window(tmp_path):
    conn = get_connection(tmp_path / "radiobeacon.db")
    add_tx_schedule_unit(conn, "csn", "1", "voice", "", "urgent", "e1")
    config = WindowConfig(total_seconds=90, voice_seconds=60, frame_seconds=30)
    controller = _RecordingServiceController()

    prepped_voice, _ = main_module._maybe_control_services(
        conn, config, 89.0, controller, "svxlink", "direwolf",
        lead_time=2.0, prepped_voice=True, prepped_frame=False,
    )

    assert prepped_voice is True
    assert controller.calls == []


def test_maybe_control_services_resets_flag_outside_lead_time_window(tmp_path):
    conn = get_connection(tmp_path / "radiobeacon.db")
    config = WindowConfig(total_seconds=90, voice_seconds=60, frame_seconds=30)
    controller = _RecordingServiceController()

    prepped_voice, _ = main_module._maybe_control_services(
        conn, config, 30.0, controller, "svxlink", "direwolf",
        lead_time=2.0, prepped_voice=True, prepped_frame=False,
    )

    assert prepped_voice is False


# --- _drain_kind ---


class _StubVoiceTransmitter:
    def __init__(self):
        self.calls = []

    def transmit(self, *, text, wav_path):
        self.calls.append(text)
        return True


class _StubKissClient:
    def __init__(self, result=True):
        self.result = result
        self.calls = []

    def send_ui_frame(self, *, source_callsign, dest_callsign, info):
        self.calls.append((source_callsign, dest_callsign, info))
        return self.result


def test_drain_kind_transmits_due_row_and_increments_sent_count(tmp_path):
    conn = get_connection(tmp_path / "radiobeacon.db")
    _insert_chunk(conn, "csn", "1", 0, "chunk text")
    add_tx_schedule_unit(conn, "csn", "1", "frame", "0", "urgent", "e1")
    kiss = _StubKissClient()

    n = main_module._drain_kind(
        threading.Event(), conn, "frame", datetime(2026, 1, 1, tzinfo=timezone.utc),
        _ctx(kiss_client=kiss), 0.0,
    )

    assert n == 1
    assert kiss.calls == [("CD3DXZ-1", "WXALRT", b"chunk text")]
    row = conn.execute(
        "SELECT sent_count, last_transmitted_at FROM beacon_tx_schedule WHERE kind='frame'"
    ).fetchone()
    assert row[0] == 1
    assert row[1] is not None


def test_drain_kind_retires_row_at_repeat_times(tmp_path):
    conn = get_connection(tmp_path / "radiobeacon.db")
    _insert_chunk(conn, "csn", "1", 0, "chunk text")
    add_tx_schedule_unit(conn, "csn", "1", "frame", "0", "informational", "e1")  # 1x / 0s
    kiss = _StubKissClient()

    main_module._drain_kind(
        threading.Event(), conn, "frame", datetime(2026, 1, 1, tzinfo=timezone.utc),
        _ctx(kiss_client=kiss), 0.0,
    )

    assert _count(conn, "frame") == 0  # retired after its single transmission
    assert conn.execute(
        "SELECT 1 FROM audit_log WHERE event_type='beacon.tx.retired'"
    ).fetchone() is not None


def test_drain_kind_respects_interval_seconds(tmp_path):
    conn = get_connection(tmp_path / "radiobeacon.db")
    _insert_chunk(conn, "csn", "1", 0, "chunk text")
    set_policy(conn, "slow", repeat_times=5, interval_seconds=60)
    add_tx_schedule_unit(conn, "csn", "1", "frame", "0", "slow", "e1")
    kiss = _StubKissClient()

    base = datetime(2026, 1, 1, tzinfo=timezone.utc)
    # first pass: due (never transmitted)
    assert main_module._drain_kind(threading.Event(), conn, "frame", base, _ctx(kiss_client=kiss), 0.0) == 1
    # 30s later: not due (interval 60s)
    assert main_module._drain_kind(
        threading.Event(), conn, "frame", base + timedelta(seconds=30), _ctx(kiss_client=kiss), 0.0
    ) == 0
    # 61s later: due again
    assert main_module._drain_kind(
        threading.Event(), conn, "frame", base + timedelta(seconds=61), _ctx(kiss_client=kiss), 0.0
    ) == 1
    assert len(kiss.calls) == 2


def test_drain_kind_counts_failed_attempts_against_budget(tmp_path):
    conn = get_connection(tmp_path / "radiobeacon.db")
    _insert_chunk(conn, "csn", "1", 0, "chunk text")
    set_policy(conn, "twice", repeat_times=2, interval_seconds=0)
    add_tx_schedule_unit(conn, "csn", "1", "frame", "0", "twice", "e1")
    kiss = _StubKissClient(result=False)  # every send fails

    base = datetime(2026, 1, 1, tzinfo=timezone.utc)
    main_module._drain_kind(threading.Event(), conn, "frame", base, _ctx(kiss_client=kiss), 0.0)
    main_module._drain_kind(threading.Event(), conn, "frame", base, _ctx(kiss_client=kiss), 0.0)

    # two attempts, both failed, row still retired
    assert _count(conn, "frame") == 0


def test_drain_kind_stops_early_on_stop_event(tmp_path):
    conn = get_connection(tmp_path / "radiobeacon.db")
    for i in range(5):
        _insert_chunk(conn, "csn", str(i), 0, "chunk text")
        add_tx_schedule_unit(conn, "csn", str(i), "frame", "0", "urgent", "e1")

    stop_event = threading.Event()
    stop_event.set()

    n = main_module._drain_kind(
        stop_event, conn, "frame", datetime(2026, 1, 1, tzinfo=timezone.utc),
        _ctx(kiss_client=_StubKissClient()), 0.0,
    )

    assert n == 0


def test_drain_kind_is_generic_over_kinds(tmp_path):
    """A new kind just needs a SLOT_KINDS + KIND_TRANSMITTERS entry — the
    drain loop itself never special-cases."""
    conn = get_connection(tmp_path / "radiobeacon.db")
    seen = []
    main_module.KIND_TRANSMITTERS["telemetry"] = lambda c, row, ctx: seen.append(row["item_id"]) or True
    try:
        add_tx_schedule_unit(conn, "csn", "42", "telemetry", "", "informational", "e1")
        n = main_module._drain_kind(
            threading.Event(), conn, "telemetry", datetime(2026, 1, 1, tzinfo=timezone.utc), _ctx(), 0.0
        )
        assert n == 1
        assert seen == ["42"]
        assert _count(conn, "telemetry") == 0
    finally:
        del main_module.KIND_TRANSMITTERS["telemetry"]


def test_drain_kind_survives_reopened_connection(tmp_path):
    """The schedule table is the durable source of truth — a restart
    (new connection) still sees pending rows."""
    db = tmp_path / "radiobeacon.db"
    conn = get_connection(db)
    _insert_chunk(conn, "csn", "1", 0, "chunk text")
    set_policy(conn, "twice", repeat_times=2, interval_seconds=0)
    add_tx_schedule_unit(conn, "csn", "1", "frame", "0", "twice", "e1")
    kiss = _StubKissClient()
    main_module._drain_kind(threading.Event(), conn, "frame", datetime(2026, 1, 1, tzinfo=timezone.utc), _ctx(kiss_client=kiss), 0.0)
    conn.close()

    conn2 = get_connection(db)
    assert _count(conn2, "frame") == 1  # still one pass left
    main_module._drain_kind(threading.Event(), conn2, "frame", datetime(2026, 1, 1, 0, 1, tzinfo=timezone.utc), _ctx(kiss_client=kiss), 0.0)
    assert _count(conn2, "frame") == 0


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


# --- transmit units ---


def test_transmit_voice_unit_skips_when_no_callsign(tmp_path):
    conn = get_connection(tmp_path / "radiobeacon.db")
    _insert_item(conn, "csn", "1", extracted_contents="raw contents")
    transmitter = _StubVoiceTransmitter()

    sent = main_module._transmit_voice_unit(
        conn, {"source": "csn", "item_id": "1", "ref": ""},
        _ctx(callsign=None, voice_transmitter=transmitter),
    )

    assert sent is False
    assert transmitter.calls == []
    assert conn.execute(
        "SELECT 1 FROM audit_log WHERE event_type = 'beacon.voice.skipped_no_callsign'"
    ).fetchone() is not None


def test_transmit_voice_unit_uses_summary_not_extracted_contents(tmp_path, monkeypatch):
    synth = []
    monkeypatch.setattr("beacon.voice.synthesize_speech", lambda text, **k: synth.append(text) or True)
    conn = get_connection(tmp_path / "radiobeacon.db")
    _insert_item(
        conn, "csn", "1",
        extracted_contents="Raw sensor payload -- should never be spoken.",
        summary="Sismo de magnitud 4.2.",
    )
    transmitter = _StubVoiceTransmitter()

    main_module._transmit_voice_unit(
        conn, {"source": "csn", "item_id": "1", "ref": ""},
        _ctx(voice_transmitter=transmitter),
    )

    assert synth == ["CD3DXZ-1. Sismo de magnitud 4.2."]
    assert transmitter.calls == ["CD3DXZ-1. Sismo de magnitud 4.2."]
    assert conn.execute(
        "SELECT 1 FROM audit_log WHERE event_type = 'beacon.voice.transmitted'"
    ).fetchone() is not None


def test_transmit_voice_unit_applies_prefix_and_suffix(tmp_path, monkeypatch):
    monkeypatch.setattr("beacon.voice.synthesize_speech", lambda *a, **k: True)
    conn = get_connection(tmp_path / "radiobeacon.db")
    _insert_item(conn, "csn", "1", summary="Sismo de magnitud 4.2.")
    transmitter = _StubVoiceTransmitter()

    main_module._transmit_voice_unit(
        conn, {"source": "csn", "item_id": "1", "ref": ""},
        _ctx(voice_template="{text}", voice_prefix=">> ", voice_suffix=" <<", voice_transmitter=transmitter),
    )

    assert transmitter.calls == [">> Sismo de magnitud 4.2. <<"]


def test_transmit_frame_unit_skips_when_no_callsign(tmp_path):
    conn = get_connection(tmp_path / "radiobeacon.db")
    _insert_chunk(conn, "csn", "1", 0, "chunk text")
    kiss = _StubKissClient()

    sent = main_module._transmit_frame_unit(
        conn, {"source": "csn", "item_id": "1", "ref": "0"}, _ctx(callsign=None, kiss_client=kiss)
    )

    assert sent is False
    assert kiss.calls == []


def test_transmit_frame_unit_sends_and_records_audit_event(tmp_path):
    conn = get_connection(tmp_path / "radiobeacon.db")
    _insert_chunk(conn, "csn", "1", 0, "chunk text")
    kiss = _StubKissClient()

    main_module._transmit_frame_unit(
        conn, {"source": "csn", "item_id": "1", "ref": "0"}, _ctx(kiss_client=kiss)
    )

    assert kiss.calls == [("CD3DXZ-1", "WXALRT", b"chunk text")]
    assert conn.execute(
        "SELECT 1 FROM audit_log WHERE event_type = 'beacon.frame.transmitted'"
    ).fetchone() is not None


def test_transmit_frame_unit_records_dropped_too_long(tmp_path):
    conn = get_connection(tmp_path / "radiobeacon.db")
    _insert_chunk(conn, "csn", "1", 0, "x" * 300)
    kiss = _StubKissClient()

    main_module._transmit_frame_unit(
        conn, {"source": "csn", "item_id": "1", "ref": "0"}, _ctx(kiss_client=kiss)
    )

    assert kiss.calls == []
    assert conn.execute(
        "SELECT 1 FROM audit_log WHERE event_type = 'beacon.frame.dropped_too_long'"
    ).fetchone() is not None


def test_transmit_frame_unit_applies_prefix_and_suffix(tmp_path):
    conn = get_connection(tmp_path / "radiobeacon.db")
    _insert_chunk(conn, "csn", "1", 0, "chunk text")
    kiss = _StubKissClient()

    main_module._transmit_frame_unit(
        conn, {"source": "csn", "item_id": "1", "ref": "0"},
        _ctx(kiss_client=kiss, frame_prefix=">> ", frame_suffix=" [EXPERIMENTAL]"),
    )

    assert kiss.calls == [("CD3DXZ-1", "WXALRT", b">> chunk text [EXPERIMENTAL]")]


def test_transmit_frame_unit_records_transmit_failed_on_send_failure(tmp_path):
    conn = get_connection(tmp_path / "radiobeacon.db")
    _insert_chunk(conn, "csn", "1", 0, "chunk text")

    main_module._transmit_frame_unit(
        conn, {"source": "csn", "item_id": "1", "ref": "0"},
        _ctx(kiss_client=_StubKissClient(result=False)),
    )

    assert conn.execute(
        "SELECT 1 FROM audit_log WHERE event_type = 'beacon.frame.transmit_failed'"
    ).fetchone() is not None


# --- factories ---


def test_build_voice_transmitter_defaults_to_logging(tmp_path):
    conn = get_connection(tmp_path / "radiobeacon.db")
    assert type(main_module._build_voice_transmitter(conn)).__name__ == "LoggingVoiceTransmitter"


def test_build_service_controller_defaults_to_logging(tmp_path):
    conn = get_connection(tmp_path / "radiobeacon.db")
    assert type(main_module._build_service_controller(conn)).__name__ == "LoggingServiceController"


def test_build_service_controller_systemctl_when_configured(tmp_path):
    conn = get_connection(tmp_path / "radiobeacon.db")
    set_setting(conn, "BEACON_SERVICE_CONTROLLER", "systemctl")
    assert type(main_module._build_service_controller(conn)).__name__ == "SystemctlServiceController"


def test_run_mqtt_client_uses_stable_client_id_and_clean_session_true(monkeypatch):
    import paho.mqtt.client

    captured = {}

    class FakeClientForConnect:
        def __init__(self, callback_api_version, client_id=None, clean_session=None):
            captured["client_id"] = client_id
            captured["clean_session"] = clean_session

    monkeypatch.setattr(paho.mqtt.client, "Client", FakeClientForConnect)

    stop_event = threading.Event()
    stop_event.set()

    main_module._run_mqtt_client(
        "radiobeacon/events/item.content_ready", stop_event, threading.Event(),
    )

    assert captured["client_id"] == "radiobeacon-beacon"
    assert captured["clean_session"] is True
