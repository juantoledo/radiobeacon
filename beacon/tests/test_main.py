import json
import threading
import uuid
from datetime import datetime, timedelta, timezone

import pytest
from adapters.storage import (
    add_tx_schedule_unit,
    enqueue_manual_tx,
    get_beacon_status,
    get_connection,
    mark_item_ready_published,
    pending_manual_tx,
    record_audit_event,
    set_adapter_instance,
    set_setting,
)
from adapters.policy import set_policy

import beacon.__main__ as main_module


class FakeMessage:
    def __init__(self, topic: str, payload: bytes):
        self.topic = topic
        self.payload = payload


class FakeClient:
    def __init__(self):
        self.published = []

    def publish(self, topic, payload=None, qos=0):
        self.published.append((topic, payload, qos))


class _RecordingWavTransmitter:
    def __init__(self, result=True):
        self.result = result
        self.calls = []

    def transmit(self, *, wav_path, label):
        self.calls.append((str(wav_path), label))
        return self.result


@pytest.fixture(autouse=True)
def _stub_frame_audio(monkeypatch):
    """gen_packets isn't available in the test env — stub the render step so
    _transmit_frame_unit / _drain_kind exercise everything around it. Returns
    the list of TNC2 lines that would have been rendered."""
    rendered = []

    def _fake(tnc2_line, *, out_path, gen_packets_binary="gen_packets", lead_silence_ms=250):
        rendered.append(tnc2_line)
        return True

    monkeypatch.setattr(main_module.frame_audio, "synthesize_frame_wav", _fake)
    return rendered


@pytest.fixture(autouse=True)
def _stub_voice_synthesis(monkeypatch):
    """Default: TTS succeeds without touching espeak/piper. Individual tests
    override with their own monkeypatch when they need to inspect the text."""
    monkeypatch.setattr("beacon.voice.synthesize_speech", lambda *a, **k: True)


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
    extracted_title=None, url=None, item_type=None, subtype=None, policy=None,
):
    conn.execute(
        "INSERT INTO items (source, item_id, extracted_contents, summary, source_date_time, "
        "extracted_title, url, type, subtype, policy, fetched_at, rawdata) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, datetime('now'), '{}')",
        (source, item_id, extracted_contents, summary, source_date_time, extracted_title,
         url, item_type, subtype, policy),
    )
    conn.commit()


def _set_type(conn, beacon_type):
    set_setting(conn, "BEACON_TYPE", beacon_type)


def _count(conn, kind):
    return conn.execute(
        "SELECT COUNT(*) FROM beacon_tx_schedule WHERE kind = ?", (kind,)
    ).fetchone()[0]


CONTENT_READY_TOPIC = "radiobeacon/events/item.content_ready"


def _ctx(**overrides):
    ctx = {
        "callsign": "N0CALL-1",
        "voice_template": "{callsign}. {text}",
        "voice_max_chars": 200,
        "date_format": "%d-%m-%Y %H:%M",
        "destination": "WXALRT",
        "frame_prefix": "",
        "frame_suffix": "",
        "voice_prefix": "",
        "voice_suffix": "",
        "voice_attention_tone": "",
        "wav_dir": "/tmp",
        "tts_voice": "es",
        "tts_engine": "espeak",
        "tts_piper_model": "",
        "tts_piper_binary": "piper",
        "gen_packets_binary": "gen_packets",
        "frame_lead_silence_ms": 0,
        "watermark_voice_template": "{callsign} watermark {date}",
        "watermark_frame_template": "{callsign} watermark {date}",
        "manual_voice_template": "Aquí {callsign}. {text}",
        "wav_transmitter": _RecordingWavTransmitter(),
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


# --- BEACON_TYPE resolution ---


def test_resolve_beacon_type_defaults_to_voice(tmp_path):
    conn = get_connection(tmp_path / "radiobeacon.db")
    assert main_module._resolve_beacon_type(conn) == "voice"


def test_resolve_beacon_type_reads_setting(tmp_path):
    conn = get_connection(tmp_path / "radiobeacon.db")
    _set_type(conn, "frame")
    assert main_module._resolve_beacon_type(conn) == "frame"


def test_resolve_beacon_type_falls_back_on_garbage(tmp_path):
    conn = get_connection(tmp_path / "radiobeacon.db")
    _set_type(conn, "morse")
    assert main_module._resolve_beacon_type(conn) == "voice"


# --- BEACON_TTS_WAV_DIR resolution ---


def test_resolve_wav_dir_anchors_relative_path_at_repo_root():
    resolved = main_module._resolve_wav_dir("storage/beacon_tts")
    assert resolved == str(main_module.REPO_ROOT / "storage" / "beacon_tts")


def test_resolve_wav_dir_keeps_absolute_path(tmp_path):
    assert main_module._resolve_wav_dir(str(tmp_path)) == str(tmp_path)


def test_prune_old_wavs_removes_only_stale_files(tmp_path):
    import os
    import time

    old = tmp_path / "csn-1-100.wav"
    fresh = tmp_path / "csn-1-200.wav"
    other = tmp_path / "notes.txt"
    for p in (old, fresh, other):
        p.write_bytes(b"x")
    now = time.time()
    os.utime(old, (now - 40 * 86400, now - 40 * 86400))

    removed = main_module._prune_old_wavs(tmp_path, retention_days=30, now=now)

    assert removed == 1
    assert not old.exists()
    assert fresh.exists() and other.exists()


def test_prune_old_wavs_noop_when_disabled(tmp_path):
    import os
    import time

    stale = tmp_path / "csn-1-100.wav"
    stale.write_bytes(b"x")
    now = time.time()
    os.utime(stale, (now - 999 * 86400, now - 999 * 86400))

    assert main_module._prune_old_wavs(tmp_path, retention_days=0, now=now) == 0
    assert stale.exists()


# --- manual transmission (beacon_manual_tx) ---


def _audit_types(conn):
    return [r[0] for r in conn.execute("SELECT event_type FROM audit_log ORDER BY id")]


def test_drain_manual_tx_transmits_voice_and_deletes_row(tmp_path, monkeypatch):
    spoken = []
    monkeypatch.setattr(
        "beacon.voice.synthesize_speech",
        lambda text, **k: (spoken.append(text) or True),
    )
    conn = get_connection(tmp_path / "radiobeacon.db")
    enqueue_manual_tx(conn, kind="voice", text="Prueba manual", actor="ui.dashboard")
    ctx = _ctx(wav_dir=str(tmp_path))
    stop = threading.Event()

    attempted = main_module._drain_manual_tx(
        stop, conn, "voice", datetime.now(timezone.utc), ctx, 0.0
    )

    assert attempted == 1
    assert spoken == ["Aquí N0CALL-1. Prueba manual"]
    assert ctx["wav_transmitter"].calls
    assert pending_manual_tx(conn, "voice") == []
    assert "beacon.manual.transmitted" in _audit_types(conn)
    assert get_beacon_status(conn, "last_manual_transmit_at") is not None


def test_drain_manual_tx_voice_prepends_attention_tone(tmp_path, monkeypatch):
    monkeypatch.setattr("beacon.voice.synthesize_speech", lambda *a, **k: True)
    tones = []
    monkeypatch.setattr(main_module, "prepend_tone_to_wav", lambda wav_path, spec, **k: tones.append(spec))
    conn = get_connection(tmp_path / "radiobeacon.db")
    enqueue_manual_tx(conn, kind="voice", text="Prueba", actor="ui.dashboard")
    ctx = _ctx(wav_dir=str(tmp_path), voice_attention_tone="900:150,0:80,900:150")

    main_module._drain_manual_tx(
        threading.Event(), conn, "voice", datetime.now(timezone.utc), ctx, 0.0
    )

    assert tones == ["900:150,0:80,900:150"]
    assert ctx["wav_transmitter"].calls


def test_drain_manual_tx_leaves_other_kind_queued(tmp_path):
    conn = get_connection(tmp_path / "radiobeacon.db")
    enqueue_manual_tx(conn, kind="frame", text="solo frame", actor="ui.dashboard")

    attempted = main_module._drain_manual_tx(
        threading.Event(), conn, "voice", datetime.now(timezone.utc), _ctx(wav_dir=str(tmp_path)), 0.0
    )

    assert attempted == 0
    assert len(pending_manual_tx(conn, "frame")) == 1


def test_drain_manual_tx_drops_overlong_frame(tmp_path, _stub_frame_audio):
    conn = get_connection(tmp_path / "radiobeacon.db")
    enqueue_manual_tx(conn, kind="frame", text="x" * 400, actor="ui.dashboard")

    main_module._drain_manual_tx(
        threading.Event(), conn, "frame", datetime.now(timezone.utc), _ctx(wav_dir=str(tmp_path)), 0.0
    )

    assert pending_manual_tx(conn, "frame") == []
    assert _stub_frame_audio == []  # never reached the render step
    assert "beacon.manual.dropped_too_long" in _audit_types(conn)


def test_drain_manual_tx_deletes_row_even_when_transmitter_fails(tmp_path):
    conn = get_connection(tmp_path / "radiobeacon.db")
    enqueue_manual_tx(conn, kind="voice", text="hola", actor="ui.dashboard")
    ctx = _ctx(wav_dir=str(tmp_path), wav_transmitter=_RecordingWavTransmitter(result=False))

    main_module._drain_manual_tx(
        threading.Event(), conn, "voice", datetime.now(timezone.utc), ctx, 0.0
    )

    assert pending_manual_tx(conn, "voice") == []
    assert "beacon.manual.transmit_failed" in _audit_types(conn)


# --- content_ready -> schedule rows ---


def test_handle_content_ready_frame_type_schedules_one_row_per_chunk(tmp_path):
    conn = get_connection(tmp_path / "radiobeacon.db")
    _set_type(conn, "frame")
    _insert_item(conn, "csn", "1", policy="informational")
    _insert_chunk(conn, "csn", "1", 0, "chunk zero", chunk_count=3)
    _insert_chunk(conn, "csn", "1", 1, "chunk one", chunk_count=3)
    _insert_chunk(conn, "csn", "1", 2, "chunk two", chunk_count=3)

    main_module._handle_content_ready_event(conn, "csn", "1", "event-1")

    assert _count(conn, "frame") == 3
    assert _count(conn, "voice") == 0
    frame0 = conn.execute(
        "SELECT ref, policy, sent_count FROM beacon_tx_schedule "
        "WHERE kind='frame' AND ref='0'"
    ).fetchone()
    assert frame0 == ("0", "informational", 0)


def test_handle_content_ready_voice_type_schedules_one_voice_row_only(tmp_path):
    conn = get_connection(tmp_path / "radiobeacon.db")
    _set_type(conn, "voice")
    _insert_item(conn, "csn", "1")
    _insert_chunk(conn, "csn", "1", 0, "chunk zero")

    main_module._handle_content_ready_event(conn, "csn", "1", "event-1")

    assert _count(conn, "frame") == 0
    assert _count(conn, "voice") == 1


def test_handle_content_ready_frame_type_no_chunks_yet_schedules_nothing(tmp_path):
    conn = get_connection(tmp_path / "radiobeacon.db")
    _set_type(conn, "frame")
    _insert_item(conn, "csn", "1")

    main_module._handle_content_ready_event(conn, "csn", "1", "event-1")

    assert _count(conn, "frame") == 0
    assert _count(conn, "voice") == 0


def test_handle_content_ready_event_idempotent_on_same_event_id(tmp_path):
    conn = get_connection(tmp_path / "radiobeacon.db")
    _set_type(conn, "frame")
    _insert_item(conn, "csn", "1")
    _insert_chunk(conn, "csn", "1", 0, "chunk zero")

    main_module._handle_content_ready_event(conn, "csn", "1", "event-1")
    main_module._handle_content_ready_event(conn, "csn", "1", "event-1")

    assert _count(conn, "frame") == 1


def test_handle_content_ready_event_rearm_with_new_event_id_resets_rows(tmp_path):
    conn = get_connection(tmp_path / "radiobeacon.db")
    _set_type(conn, "frame")
    _insert_item(conn, "csn", "1")
    _insert_chunk(conn, "csn", "1", 0, "chunk zero")

    main_module._handle_content_ready_event(conn, "csn", "1", "event-1")
    conn.execute("UPDATE beacon_tx_schedule SET sent_count = 3, last_transmitted_at = '2020-01-01T00:00:00'")
    conn.commit()

    main_module._handle_content_ready_event(conn, "csn", "1", "event-2")

    assert _count(conn, "frame") == 1
    row = conn.execute(
        "SELECT sent_count, last_transmitted_at, enqueued_event_id FROM beacon_tx_schedule "
        "WHERE kind='frame'"
    ).fetchone()
    assert row == (0, None, "event-2")


def test_handle_content_ready_event_records_audit_event(tmp_path):
    conn = get_connection(tmp_path / "radiobeacon.db")
    _set_type(conn, "frame")
    _insert_item(conn, "csn", "1")
    _insert_chunk(conn, "csn", "1", 0, "chunk zero")

    main_module._handle_content_ready_event(conn, "csn", "1", "event-1")

    row = conn.execute(
        "SELECT details FROM audit_log WHERE event_type = 'beacon.content_ready.enqueued'"
    ).fetchone()
    details = json.loads(row[0])
    assert details["event_id"] == "event-1"
    assert details["beacon_type"] == "frame"
    assert details["frame_count"] == 1


def test_handle_content_ready_event_sets_wake_event(tmp_path):
    conn = get_connection(tmp_path / "radiobeacon.db")
    _insert_item(conn, "csn", "1")
    wake_event = threading.Event()

    main_module._handle_content_ready_event(conn, "csn", "1", "event-1", wake_event)

    assert wake_event.is_set()


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
    _set_type(conn, "voice")
    _insert_item(conn, "csn", "1")
    mark_item_ready_published(conn, "csn", "1")

    reconciled = main_module._reconcile_missed_content_ready(conn)

    assert reconciled == 1
    assert _count(conn, "voice") == 1


def test_reconcile_skips_item_already_scheduled_via_live_mqtt(tmp_path):
    conn = get_connection(tmp_path / "radiobeacon.db")
    _insert_item(conn, "csn", "1")
    main_module._handle_content_ready_event(conn, "csn", "1", "event-1")
    mark_item_ready_published(conn, "csn", "1")

    assert main_module._reconcile_missed_content_ready(conn) == 0


def test_reconcile_is_a_noop_with_nothing_pending(tmp_path):
    conn = get_connection(tmp_path / "radiobeacon.db")

    assert main_module._reconcile_missed_content_ready(conn) == 0


def test_reconcile_reschedules_after_a_rearm_republish(tmp_path):
    conn = get_connection(tmp_path / "radiobeacon.db")
    _insert_item(conn, "csn", "1")
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
    _set_type(conn, "frame")
    _insert_item(conn, "senapred", "1")
    _insert_chunk(conn, "senapred", "1", 0, "chunk zero")

    handler = main_module._make_on_message(CONTENT_READY_TOPIC, threading.Event())
    handler(FakeClient(), None, FakeMessage(CONTENT_READY_TOPIC, _content_ready_payload()))

    assert _count(conn, "frame") == 1


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


def test_write_heartbeat_persists_type_and_schedule_counts(tmp_path):
    conn = get_connection(tmp_path / "radiobeacon.db")
    add_tx_schedule_unit(conn, "csn", "1", "voice", "", "informational", "e1")
    add_tx_schedule_unit(conn, "csn", "1", "frame", "0", "informational", "e1")
    add_tx_schedule_unit(conn, "csn", "1", "frame", "1", "informational", "e1")

    main_module._write_heartbeat(conn, "frame")

    status = {
        row[0]: row[1]
        for row in conn.execute("SELECT key, value FROM beacon_status").fetchall()
    }
    assert status["beacon_type"] == "frame"
    assert status["voice_queue_depth"] == "1"
    assert status["frame_queue_depth"] == "2"
    assert status["process_heartbeat_at"]


# --- _drain_kind ---


def test_drain_kind_transmits_due_frame_row_and_increments_sent_count(tmp_path, _stub_frame_audio):
    conn = get_connection(tmp_path / "radiobeacon.db")
    _insert_chunk(conn, "csn", "1", 0, "chunk text")
    set_policy(conn, "repeat", transmit_kind="interval", transmit_count=3, transmit_interval_seconds=60)
    add_tx_schedule_unit(conn, "csn", "1", "frame", "0", "repeat", "e1")
    tx = _RecordingWavTransmitter()

    n = main_module._drain_kind(
        threading.Event(), conn, "frame", datetime(2026, 1, 1, tzinfo=timezone.utc),
        _ctx(wav_transmitter=tx), 0.0,
    )

    assert n == 1
    assert _stub_frame_audio == ["N0CALL-1>WXALRT:chunk text"]
    assert len(tx.calls) == 1
    row = conn.execute(
        "SELECT sent_count, last_transmitted_at FROM beacon_tx_schedule WHERE kind='frame'"
    ).fetchone()
    assert row[0] == 1
    assert row[1] is not None


def test_drain_kind_transmits_due_voice_row(tmp_path):
    conn = get_connection(tmp_path / "radiobeacon.db")
    _insert_item(conn, "csn", "1", summary="hola mundo")
    add_tx_schedule_unit(conn, "csn", "1", "voice", "", "informational", "e1")
    tx = _RecordingWavTransmitter()

    n = main_module._drain_kind(
        threading.Event(), conn, "voice", datetime(2026, 1, 1, tzinfo=timezone.utc),
        _ctx(wav_transmitter=tx), 0.0,
    )

    assert n == 1
    assert len(tx.calls) == 1
    assert tx.calls[0][1] == "voice csn/1"


def test_drain_kind_retires_row_at_repeat_times(tmp_path):
    conn = get_connection(tmp_path / "radiobeacon.db")
    _insert_chunk(conn, "csn", "1", 0, "chunk text")
    add_tx_schedule_unit(conn, "csn", "1", "frame", "0", "informational", "e1")  # 1x / 0s

    main_module._drain_kind(
        threading.Event(), conn, "frame", datetime(2026, 1, 1, tzinfo=timezone.utc), _ctx(), 0.0,
    )

    assert _count(conn, "frame") == 0
    assert conn.execute(
        "SELECT 1 FROM audit_log WHERE event_type='beacon.tx.retired'"
    ).fetchone() is not None


def test_drain_kind_respects_interval_seconds(tmp_path):
    conn = get_connection(tmp_path / "radiobeacon.db")
    _insert_chunk(conn, "csn", "1", 0, "chunk text")
    set_policy(conn, "slow", transmit_kind="interval", transmit_count=5, transmit_interval_seconds=60)
    add_tx_schedule_unit(conn, "csn", "1", "frame", "0", "slow", "e1")

    base = datetime(2026, 1, 1, tzinfo=timezone.utc)
    assert main_module._drain_kind(threading.Event(), conn, "frame", base, _ctx(), 0.0) == 1
    assert main_module._drain_kind(
        threading.Event(), conn, "frame", base + timedelta(seconds=30), _ctx(), 0.0
    ) == 0
    assert main_module._drain_kind(
        threading.Event(), conn, "frame", base + timedelta(seconds=61), _ctx(), 0.0
    ) == 1


def test_drain_kind_counts_failed_attempts_against_budget(tmp_path):
    conn = get_connection(tmp_path / "radiobeacon.db")
    _insert_chunk(conn, "csn", "1", 0, "chunk text")
    set_policy(conn, "twice", transmit_kind="interval", transmit_count=2, transmit_interval_seconds=30)
    add_tx_schedule_unit(conn, "csn", "1", "frame", "0", "twice", "e1")
    tx = _RecordingWavTransmitter(result=False)  # every hand-off fails

    base = datetime(2026, 1, 1, tzinfo=timezone.utc)
    main_module._drain_kind(threading.Event(), conn, "frame", base, _ctx(wav_transmitter=tx), 0.0)
    main_module._drain_kind(
        threading.Event(), conn, "frame", base + timedelta(seconds=31), _ctx(wav_transmitter=tx), 0.0
    )

    assert _count(conn, "frame") == 0


def test_drain_kind_stops_early_on_stop_event(tmp_path):
    conn = get_connection(tmp_path / "radiobeacon.db")
    for i in range(5):
        _insert_chunk(conn, "csn", str(i), 0, "chunk text")
        add_tx_schedule_unit(conn, "csn", str(i), "frame", "0", "informational", "e1")

    stop_event = threading.Event()
    stop_event.set()

    n = main_module._drain_kind(
        stop_event, conn, "frame", datetime(2026, 1, 1, tzinfo=timezone.utc), _ctx(), 0.0,
    )

    assert n == 0


def test_drain_kind_is_generic_over_kinds(tmp_path):
    """A new kind just needs a KIND_TRANSMITTERS entry — the drain loop itself
    never special-cases."""
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


# --- _purge_stale_rows ---


def _backdate_tx_row(conn, source, item_id, kind, ref, updated_at):
    conn.execute(
        "UPDATE beacon_tx_schedule SET updated_at = ? "
        "WHERE source = ? AND item_id = ? AND kind = ? AND ref = ?",
        (updated_at, source, item_id, kind, ref),
    )
    conn.commit()


def test_purge_stale_rows_drops_aged_row_and_audits(tmp_path):
    conn = get_connection(tmp_path / "radiobeacon.db")
    add_tx_schedule_unit(conn, "csn", "old", "voice", "", "informational", "e1")
    add_tx_schedule_unit(conn, "csn", "fresh", "voice", "", "informational", "e2")
    _backdate_tx_row(conn, "csn", "old", "voice", "", "2026-09-03 00:00:00")

    now_dt = datetime(2026, 9, 3, 12, 0, tzinfo=timezone.utc)
    dropped = main_module._purge_stale_rows(conn, "voice", 3600, now_dt)

    assert dropped == 1
    assert _count(conn, "voice") == 1
    assert conn.execute(
        "SELECT COUNT(*) FROM beacon_tx_schedule WHERE item_id = 'fresh'"
    ).fetchone()[0] == 1
    ev = conn.execute(
        "SELECT source, item_id, details FROM audit_log WHERE event_type = 'beacon.tx.skipped_stale'"
    ).fetchall()
    assert len(ev) == 1
    assert (ev[0][0], ev[0][1]) == ("csn", "old")
    assert json.loads(ev[0][2])["max_age_seconds"] == 3600


def test_purge_stale_rows_noop_when_age_limit_zero(tmp_path):
    conn = get_connection(tmp_path / "radiobeacon.db")
    add_tx_schedule_unit(conn, "csn", "old", "voice", "", "informational", "e1")
    _backdate_tx_row(conn, "csn", "old", "voice", "", "2020-01-01 00:00:00")

    assert main_module._purge_stale_rows(
        conn, "voice", 0, datetime(2026, 9, 3, 12, 0, tzinfo=timezone.utc)
    ) == 0
    assert _count(conn, "voice") == 1


def test_purge_stale_rows_spares_a_rearmed_row(tmp_path):
    conn = get_connection(tmp_path / "radiobeacon.db")
    add_tx_schedule_unit(conn, "csn", "1", "voice", "", "informational", "e1")
    _backdate_tx_row(conn, "csn", "1", "voice", "", "2026-09-03 00:00:00")
    # a genuine rearm (new event id) refreshes updated_at to now
    add_tx_schedule_unit(conn, "csn", "1", "voice", "", "informational", "e2")

    now_dt = datetime.now(timezone.utc)
    assert main_module._purge_stale_rows(conn, "voice", 3600, now_dt) == 0
    assert _count(conn, "voice") == 1


def test_purge_stale_rows_leaves_manual_tx_untouched(tmp_path):
    conn = get_connection(tmp_path / "radiobeacon.db")
    mid = enqueue_manual_tx(conn, kind="voice", text="hold me", actor="ui.dashboard")
    conn.execute(
        "UPDATE beacon_manual_tx SET created_at = '2020-01-01 00:00:00' WHERE id = ?", (mid,)
    )
    conn.commit()

    main_module._purge_stale_rows(conn, "voice", 3600, datetime.now(timezone.utc))

    assert [r["text"] for r in pending_manual_tx(conn, "voice")] == ["hold me"]


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
    tx = _RecordingWavTransmitter()

    sent = main_module._transmit_voice_unit(
        conn, {"source": "csn", "item_id": "1", "ref": ""}, _ctx(callsign=None, wav_transmitter=tx),
    )

    assert sent is False
    assert tx.calls == []
    assert conn.execute(
        "SELECT 1 FROM audit_log WHERE event_type = 'beacon.voice.skipped_no_callsign'"
    ).fetchone() is not None


def test_transmit_voice_unit_uses_summary_and_hands_wav_to_transmitter(tmp_path, monkeypatch):
    synth = []
    monkeypatch.setattr("beacon.voice.synthesize_speech", lambda text, **k: synth.append(text) or True)
    conn = get_connection(tmp_path / "radiobeacon.db")
    _insert_item(
        conn, "csn", "1",
        extracted_contents="Raw sensor payload -- should never be spoken.",
        summary="Sismo de magnitud 4.2.",
    )
    tx = _RecordingWavTransmitter()

    main_module._transmit_voice_unit(
        conn, {"source": "csn", "item_id": "1", "ref": ""}, _ctx(wav_transmitter=tx),
    )

    assert synth == ["N0CALL-1. Sismo de magnitud 4.2."]
    assert len(tx.calls) == 1
    details = conn.execute(
        "SELECT details FROM audit_log WHERE event_type = 'beacon.voice.transmitted'"
    ).fetchone()
    assert details is not None
    payload = json.loads(details[0])
    assert payload["clip"].startswith("csn-1-") and payload["clip"].endswith(".wav")
    assert "truncated" in payload


def test_transmit_voice_unit_applies_configured_voice_replacements(tmp_path, monkeypatch):
    synth = []
    monkeypatch.setattr("beacon.voice.synthesize_speech", lambda text, **k: synth.append(text) or True)
    conn = get_connection(tmp_path / "radiobeacon.db")
    set_adapter_instance(
        conn, "csn", "api",
        {"url": "http://x", "voice_replacements": {"km": "kilómetros", "SENAPRED": "Senapred"}},
    )
    _insert_item(conn, "csn", "1", summary="Sismo a 10 km. Consulte SENAPRED.")

    main_module._transmit_voice_unit(
        conn, {"source": "csn", "item_id": "1", "ref": ""}, _ctx(wav_transmitter=_RecordingWavTransmitter()),
    )

    assert synth == ["N0CALL-1. Sismo a 10 kilómetros. Consulte Senapred."]
    # the stored summary keeps the abbreviations
    assert conn.execute(
        "SELECT summary FROM items WHERE source = 'csn' AND item_id = '1'"
    ).fetchone()[0] == "Sismo a 10 km. Consulte SENAPRED."


def test_transmit_voice_unit_leaves_text_untouched_without_a_map(tmp_path, monkeypatch):
    synth = []
    monkeypatch.setattr("beacon.voice.synthesize_speech", lambda text, **k: synth.append(text) or True)
    conn = get_connection(tmp_path / "radiobeacon.db")
    set_adapter_instance(conn, "csn", "api", {"url": "http://x"})
    _insert_item(conn, "csn", "1", summary="Sismo a 10 km.")

    main_module._transmit_voice_unit(
        conn, {"source": "csn", "item_id": "1", "ref": ""}, _ctx(wav_transmitter=_RecordingWavTransmitter()),
    )

    assert synth == ["N0CALL-1. Sismo a 10 km."]


def test_transmit_voice_unit_expands_before_truncation(tmp_path, monkeypatch):
    synth = []
    monkeypatch.setattr("beacon.voice.synthesize_speech", lambda text, **k: synth.append(text) or True)
    conn = get_connection(tmp_path / "radiobeacon.db")
    set_adapter_instance(
        conn, "csn", "api",
        {"url": "http://x", "voice_replacements": {"km": "kilometrooooos"}},
    )
    _insert_item(conn, "csn", "1", summary="uno km " + "x " * 40)

    main_module._transmit_voice_unit(
        conn, {"source": "csn", "item_id": "1", "ref": ""},
        _ctx(voice_max_chars=20, voice_template="{text}", wav_transmitter=_RecordingWavTransmitter()),
    )

    # expansion happened, then a clean word-boundary truncation to the budget
    assert synth[0].startswith("uno kilometrooooos")
    assert "km" not in synth[0]
    assert len(synth[0]) <= 20


def test_transmit_voice_unit_records_tts_failure(tmp_path, monkeypatch):
    monkeypatch.setattr("beacon.voice.synthesize_speech", lambda *a, **k: False)
    conn = get_connection(tmp_path / "radiobeacon.db")
    _insert_item(conn, "csn", "1", summary="hola")
    tx = _RecordingWavTransmitter()

    sent = main_module._transmit_voice_unit(
        conn, {"source": "csn", "item_id": "1", "ref": ""}, _ctx(wav_transmitter=tx),
    )

    assert sent is False
    assert tx.calls == []
    row = conn.execute(
        "SELECT details FROM audit_log WHERE event_type = 'beacon.voice.transmit_failed'"
    ).fetchone()
    assert json.loads(row[0])["reason"] == "tts_failed"


def test_transmit_voice_unit_prepends_attention_tone_after_synth(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr("beacon.voice.synthesize_speech", lambda *a, **k: calls.append("synth") or True)
    monkeypatch.setattr(
        main_module, "prepend_tone_to_wav",
        lambda wav_path, spec, **k: calls.append(("tone", spec)),
    )
    conn = get_connection(tmp_path / "radiobeacon.db")
    _insert_item(conn, "csn", "1", summary="hola")
    tx = _RecordingWavTransmitter()

    main_module._transmit_voice_unit(
        conn, {"source": "csn", "item_id": "1", "ref": ""},
        _ctx(wav_transmitter=tx, voice_attention_tone="1400:200"),
    )

    # tone prepended after synthesis, before the WAV reaches the transmitter
    assert calls == ["synth", ("tone", "1400:200")]
    assert len(tx.calls) == 1


def test_transmit_voice_unit_skips_tone_when_unconfigured(tmp_path, monkeypatch):
    monkeypatch.setattr("beacon.voice.synthesize_speech", lambda *a, **k: True)
    called = []
    monkeypatch.setattr(main_module, "prepend_tone_to_wav", lambda *a, **k: called.append(a))
    conn = get_connection(tmp_path / "radiobeacon.db")
    _insert_item(conn, "csn", "1", summary="hola")

    main_module._transmit_voice_unit(
        conn, {"source": "csn", "item_id": "1", "ref": ""}, _ctx(voice_attention_tone=""),
    )

    assert called == []


def test_transmit_frame_unit_skips_when_no_callsign(tmp_path, _stub_frame_audio):
    conn = get_connection(tmp_path / "radiobeacon.db")
    _insert_chunk(conn, "csn", "1", 0, "chunk text")
    tx = _RecordingWavTransmitter()

    sent = main_module._transmit_frame_unit(
        conn, {"source": "csn", "item_id": "1", "ref": "0"}, _ctx(callsign=None, wav_transmitter=tx)
    )

    assert sent is False
    assert _stub_frame_audio == []
    assert tx.calls == []


def test_transmit_frame_unit_renders_and_records_audit_event(tmp_path, _stub_frame_audio):
    conn = get_connection(tmp_path / "radiobeacon.db")
    _insert_chunk(conn, "csn", "1", 0, "chunk text")
    tx = _RecordingWavTransmitter()

    main_module._transmit_frame_unit(
        conn, {"source": "csn", "item_id": "1", "ref": "0"}, _ctx(wav_transmitter=tx)
    )

    assert _stub_frame_audio == ["N0CALL-1>WXALRT:chunk text"]
    assert len(tx.calls) == 1
    row = conn.execute(
        "SELECT details FROM audit_log WHERE event_type = 'beacon.frame.transmitted'"
    ).fetchone()
    payload = json.loads(row[0])
    assert payload["tnc2"] == "N0CALL-1>WXALRT:chunk text"
    assert payload["ref"] == 0
    assert payload["clip"].startswith("csn-1-0-") and payload["clip"].endswith(".wav")


def test_transmit_frame_unit_records_dropped_too_long(tmp_path, _stub_frame_audio):
    conn = get_connection(tmp_path / "radiobeacon.db")
    _insert_chunk(conn, "csn", "1", 0, "x" * 300)

    main_module._transmit_frame_unit(
        conn, {"source": "csn", "item_id": "1", "ref": "0"}, _ctx()
    )

    assert _stub_frame_audio == []
    assert conn.execute(
        "SELECT 1 FROM audit_log WHERE event_type = 'beacon.frame.dropped_too_long'"
    ).fetchone() is not None


def test_transmit_frame_unit_applies_prefix_and_suffix(tmp_path, _stub_frame_audio):
    conn = get_connection(tmp_path / "radiobeacon.db")
    _insert_chunk(conn, "csn", "1", 0, "chunk text")

    main_module._transmit_frame_unit(
        conn, {"source": "csn", "item_id": "1", "ref": "0"},
        _ctx(frame_prefix=">> ", frame_suffix=" [EXPERIMENTAL]"),
    )

    assert _stub_frame_audio == ["N0CALL-1>WXALRT:>> chunk text [EXPERIMENTAL]"]


def test_transmit_frame_unit_records_transmit_failed_when_render_fails(tmp_path, monkeypatch):
    monkeypatch.setattr(main_module.frame_audio, "synthesize_frame_wav", lambda *a, **k: False)
    conn = get_connection(tmp_path / "radiobeacon.db")
    _insert_chunk(conn, "csn", "1", 0, "chunk text")
    tx = _RecordingWavTransmitter()

    sent = main_module._transmit_frame_unit(
        conn, {"source": "csn", "item_id": "1", "ref": "0"}, _ctx(wav_transmitter=tx),
    )

    assert sent is False
    assert tx.calls == []
    row = conn.execute(
        "SELECT details FROM audit_log WHERE event_type = 'beacon.frame.transmit_failed'"
    ).fetchone()
    assert json.loads(row[0])["reason"] == "gen_packets_failed"


def test_transmit_frame_unit_records_transmit_failed_on_handoff_failure(tmp_path):
    conn = get_connection(tmp_path / "radiobeacon.db")
    _insert_chunk(conn, "csn", "1", 0, "chunk text")

    main_module._transmit_frame_unit(
        conn, {"source": "csn", "item_id": "1", "ref": "0"},
        _ctx(wav_transmitter=_RecordingWavTransmitter(result=False)),
    )

    assert conn.execute(
        "SELECT 1 FROM audit_log WHERE event_type = 'beacon.frame.transmit_failed'"
    ).fetchone() is not None


# --- watermark ---


def test_transmit_watermark_skips_when_no_callsign(tmp_path):
    conn = get_connection(tmp_path / "radiobeacon.db")
    tx = _RecordingWavTransmitter()

    sent = main_module._transmit_watermark(
        conn, "voice", _ctx(callsign=None, wav_transmitter=tx), datetime.now(timezone.utc),
    )

    assert sent is False
    assert tx.calls == []
    assert conn.execute(
        "SELECT 1 FROM audit_log WHERE event_type = 'beacon.watermark.skipped_no_callsign'"
    ).fetchone() is not None


def test_transmit_watermark_voice_renders_template_and_hands_wav_to_transmitter(tmp_path, monkeypatch):
    monkeypatch.setenv("DISPLAY_TIMEZONE", "UTC")
    synth = []
    monkeypatch.setattr("beacon.voice.synthesize_speech", lambda text, **k: synth.append(text) or True)
    conn = get_connection(tmp_path / "radiobeacon.db")
    tx = _RecordingWavTransmitter()
    now_dt = datetime(2026, 8, 22, 14, 30, tzinfo=timezone.utc)

    sent = main_module._transmit_watermark(
        conn, "voice",
        _ctx(watermark_voice_template="{callsign} auto {date}", wav_transmitter=tx),
        now_dt,
    )

    assert sent is True
    assert synth == ["N0CALL-1 auto 22-08-2026 14:30"]
    assert len(tx.calls) == 1
    assert tx.calls[0][1] == "watermark"
    details = conn.execute(
        "SELECT details FROM audit_log WHERE event_type = 'beacon.watermark.transmitted'"
    ).fetchone()
    assert details is not None
    parsed = json.loads(details[0])
    assert parsed["kind"] == "voice" and parsed["clip"].endswith(".wav")
    assert get_beacon_status(conn, "last_watermark_transmit_at") is not None


def test_transmit_watermark_voice_records_tts_failure(tmp_path, monkeypatch):
    monkeypatch.setattr("beacon.voice.synthesize_speech", lambda *a, **k: False)
    conn = get_connection(tmp_path / "radiobeacon.db")
    tx = _RecordingWavTransmitter()

    sent = main_module._transmit_watermark(
        conn, "voice", _ctx(wav_transmitter=tx), datetime.now(timezone.utc),
    )

    assert sent is False
    assert tx.calls == []
    row = conn.execute(
        "SELECT details FROM audit_log WHERE event_type = 'beacon.watermark.transmit_failed'"
    ).fetchone()
    assert json.loads(row[0])["reason"] == "tts_failed"


def test_transmit_watermark_frame_renders_and_records_audit_event(tmp_path, monkeypatch, _stub_frame_audio):
    monkeypatch.setenv("DISPLAY_TIMEZONE", "UTC")
    conn = get_connection(tmp_path / "radiobeacon.db")
    tx = _RecordingWavTransmitter()
    now_dt = datetime(2026, 8, 22, 14, 30, tzinfo=timezone.utc)

    sent = main_module._transmit_watermark(
        conn, "frame",
        _ctx(watermark_frame_template="watermark {date}", wav_transmitter=tx),
        now_dt,
    )

    assert sent is True
    assert _stub_frame_audio == ["N0CALL-1>WXALRT:watermark 22-08-2026 14:30"]
    assert len(tx.calls) == 1
    assert conn.execute(
        "SELECT 1 FROM audit_log WHERE event_type = 'beacon.watermark.transmitted'"
    ).fetchone() is not None


def test_transmit_watermark_frame_records_dropped_too_long(tmp_path, _stub_frame_audio):
    conn = get_connection(tmp_path / "radiobeacon.db")

    sent = main_module._transmit_watermark(
        conn, "frame", _ctx(watermark_frame_template="x" * 300), datetime.now(timezone.utc),
    )

    assert sent is False
    assert _stub_frame_audio == []
    assert conn.execute(
        "SELECT 1 FROM audit_log WHERE event_type = 'beacon.watermark.dropped_too_long'"
    ).fetchone() is not None


def test_transmit_watermark_records_transmit_failed_on_handoff_failure(tmp_path):
    conn = get_connection(tmp_path / "radiobeacon.db")

    sent = main_module._transmit_watermark(
        conn, "voice", _ctx(wav_transmitter=_RecordingWavTransmitter(result=False)), datetime.now(timezone.utc),
    )

    assert sent is False
    assert conn.execute(
        "SELECT 1 FROM audit_log WHERE event_type = 'beacon.watermark.transmit_failed'"
    ).fetchone() is not None


def test_transmit_watermark_removes_wav_after_successful_handoff(tmp_path, monkeypatch):
    def _fake_synth(text, *, out_path, **k):
        out_path.write_bytes(b"RIFF....")
        return True

    monkeypatch.setattr("beacon.voice.synthesize_speech", _fake_synth)
    conn = get_connection(tmp_path / "radiobeacon.db")
    tx = _RecordingWavTransmitter()

    sent = main_module._transmit_watermark(
        conn, "voice", _ctx(wav_dir=str(tmp_path), wav_transmitter=tx), datetime.now(timezone.utc),
    )

    assert sent is True
    assert list(tmp_path.glob("watermark-*.wav")) == []


def test_transmit_watermark_removes_wav_after_failed_handoff(tmp_path, monkeypatch):
    def _fake_synth(text, *, out_path, **k):
        out_path.write_bytes(b"RIFF....")
        return True

    monkeypatch.setattr("beacon.voice.synthesize_speech", _fake_synth)
    conn = get_connection(tmp_path / "radiobeacon.db")
    tx = _RecordingWavTransmitter(result=False)

    sent = main_module._transmit_watermark(
        conn, "voice", _ctx(wav_dir=str(tmp_path), wav_transmitter=tx), datetime.now(timezone.utc),
    )

    assert sent is False
    assert list(tmp_path.glob("watermark-*.wav")) == []


def test_transmit_watermark_placeholders_include_beacon_attributes_beyond_callsign(tmp_path, monkeypatch):
    synth = []
    monkeypatch.setattr("beacon.voice.synthesize_speech", lambda text, **k: synth.append(text) or True)
    conn = get_connection(tmp_path / "radiobeacon.db")

    main_module._transmit_watermark(
        conn, "voice",
        _ctx(watermark_voice_template="{destination} via {tts_engine}"),
        datetime.now(timezone.utc),
    )

    assert synth == ["WXALRT via espeak"]


# --- factories ---


def test_build_wav_transmitter_defaults_to_logging(tmp_path):
    conn = get_connection(tmp_path / "radiobeacon.db")
    assert type(main_module._build_wav_transmitter(conn)).__name__ == "LoggingWavTransmitter"


def test_build_wav_transmitter_spool_when_configured(tmp_path):
    conn = get_connection(tmp_path / "radiobeacon.db")
    set_setting(conn, "BEACON_WAV_TRANSMITTER", "spool")
    set_setting(conn, "BEACON_TXQUEUE_INCOMING_DIR", str(tmp_path / "incoming"))
    tx = main_module._build_wav_transmitter(conn)
    assert type(tx).__name__ == "SpoolWavTransmitter"


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
