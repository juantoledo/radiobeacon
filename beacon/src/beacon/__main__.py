"""The TDMA transmission loop — beacon's entry point.

Content flows in from two independent MQTT subscriptions (not one): frame
content comes from item.chunked (actions.chunk is always-on, no skip
logic, so this is guaranteed to eventually fire for every dispatched
item), voice content comes from item.dispatched directly, NOT
item.summarized — actions.ai structurally skips summarization for content
already at or under ACTIONS_AI_MAX_CHARS, which CSN's own short templated
contents almost always is, so depending solely on item.summarized would
leave CSN permanently voice-silent. See content.py for how the actual
text gets resolved (lazily, at transmit time, not baked in at enqueue
time) and formatters.py for why length limits reuse ACTIONS_CHUNK_MAX_CHARS/
ACTIONS_AI_MAX_CHARS instead of new beacon-specific settings.

BEACON_ENABLED (decision: soft enable/disable, not real process control —
see beacon/README.md) is re-read every tick; enqueueing from MQTT happens
unconditionally regardless of it (the "strict queue" framing) — only the
dequeue+transmit step checks it. This is also what "restart from the UI"
means in practice: the next tick just re-reads settings, no process
kill/respawn involved.

Audio-device contention between SvxLink and Direwolf is handled by
actively stopping/starting each service around its slot (see
service_control.py) — CONTEXT.md's strategy #1. Deploying Direwolf/SvxLink
themselves is out of scope for this package; both are assumed to already
run as independently controllable services (systemd units by default) on
the host."""
import logging
import signal
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO_ROOT / "data-adapters" / "src"))

from adapters.storage import (  # noqa: E402
    DEFAULT_DB_PATH,
    get_connection,
    get_setting,
    record_audit_event,
    set_beacon_status,
)
from adapters.timeutil import utc_now  # noqa: E402

from beacon import content, formatters, kiss, mq, ntp, schedule, service_control, voice  # noqa: E402
from beacon.queues import BoundedDropOldestQueue  # noqa: E402

logger = logging.getLogger(__name__)

BEACON_MQ_HOST = get_setting("BEACON_MQ_HOST", "localhost")
BEACON_MQ_PORT = int(get_setting("BEACON_MQ_PORT", "1883"))
BEACON_MQ_QOS = int(get_setting("BEACON_MQ_QOS", "1"))
BEACON_MQ_RECONNECT_BACKOFF_SECONDS = int(get_setting("BEACON_MQ_RECONNECT_BACKOFF_SECONDS", "5"))


# --- MQTT ingest: two independent subscriptions feeding two queues ---


def _already_enqueued(conn, kind: str, event_id: str | None) -> bool:
    """Same event-id-keyed idempotency as actions/__main__.py's
    _already_processed (and the same fix applied there) — a rearm
    publishes a genuinely new CloudEvent for the same (source, item_id),
    which must be enqueued again, not silently skipped."""
    if not event_id:
        return False
    row = conn.execute(
        "SELECT 1 FROM audit_log WHERE event_type = ? "
        "AND json_extract(details, '$.event_id') = ? LIMIT 1",
        (f"beacon.{kind}.enqueued", event_id),
    ).fetchone()
    return row is not None


def _handle_frame_event(conn, frame_queue: BoundedDropOldestQueue, source: str, item_id: str, event_id: str | None) -> None:
    if _already_enqueued(conn, "frame", event_id):
        logger.info("beacon: frame event_id=%s already enqueued, skipping", event_id)
        return
    rows = conn.execute(
        "SELECT chunk_index FROM chunks WHERE source = ? AND item_id = ? ORDER BY chunk_index",
        (source, item_id),
    ).fetchall()
    if not rows:
        logger.info("beacon: source=%s item_id=%s has no chunks yet, skipping", source, item_id)
        return
    for (chunk_index,) in rows:
        ok = frame_queue.put(content.QueuedFrame(source=source, item_id=item_id, chunk_index=chunk_index))
        if not ok:
            record_audit_event(
                conn, event_type="beacon.queue.dropped", actor="beacon",
                source=source, item_id=item_id, details={"queue": "frame"},
            )
    record_audit_event(
        conn, event_type="beacon.frame.enqueued", actor="beacon", source=source, item_id=item_id,
        details={"event_id": event_id, "chunk_count": len(rows)},
    )


def _handle_voice_event(conn, voice_queue: BoundedDropOldestQueue, source: str, item_id: str, event_id: str | None) -> None:
    if _already_enqueued(conn, "voice", event_id):
        logger.info("beacon: voice event_id=%s already enqueued, skipping", event_id)
        return
    ok = voice_queue.put(content.QueuedVoice(source=source, item_id=item_id))
    if not ok:
        record_audit_event(
            conn, event_type="beacon.queue.dropped", actor="beacon",
            source=source, item_id=item_id, details={"queue": "voice"},
        )
    record_audit_event(
        conn, event_type="beacon.voice.enqueued", actor="beacon", source=source, item_id=item_id,
        details={"event_id": event_id},
    )


def _make_on_message(frame_topic: str, voice_topic: str, frame_queue: BoundedDropOldestQueue, voice_queue: BoundedDropOldestQueue):
    """paho-mqtt re-raises any exception an on_message callback doesn't
    catch, silently killing loop_start()'s background thread — everything
    here is wrapped in one unconditional try/except, same as
    actions/__main__.py's _make_on_message."""

    def _handler(client, userdata, message) -> None:
        try:
            event = mq.parse_cloud_event(message.payload)
            event_id = event.get("id")
            data = event.get("data") or {}
            source, item_id = data.get("source"), data.get("item_id")
            if not source or not item_id:
                logger.warning(
                    "beacon: event missing source/item_id on %s, skipping: %r", message.topic, data
                )
                return

            conn = get_connection(DEFAULT_DB_PATH)
            try:
                if message.topic == frame_topic:
                    _handle_frame_event(conn, frame_queue, source, item_id, event_id)
                elif message.topic == voice_topic:
                    _handle_voice_event(conn, voice_queue, source, item_id, event_id)
                else:
                    logger.warning("beacon: message on unexpected topic %s", message.topic)
            finally:
                conn.close()
        except Exception:
            logger.error("beacon: failed handling message on %s", message.topic, exc_info=True)

    return _handler


def _run_mqtt_client(frame_topic: str, voice_topic: str, frame_queue: BoundedDropOldestQueue, voice_queue: BoundedDropOldestQueue, stop_event: threading.Event) -> None:
    """Mirrors actions/__main__.py's _run_action_loop: loop_start() +
    stop_event.wait() (not loop_forever(), which can't be signaled from a
    threading.Event), stable client_id + clean_session=False (a
    persistent session so a QoS-1 message published while beacon is
    offline isn't lost), resubscribe on every (re)connect."""
    import paho.mqtt.client as mqtt_client

    logger.info("beacon: mqtt starting (frame_topic=%s, voice_topic=%s)", frame_topic, voice_topic)

    client = mqtt_client.Client(
        mqtt_client.CallbackAPIVersion.VERSION2,
        client_id="radiobeacon-beacon",
        clean_session=False,
    )
    client.on_message = _make_on_message(frame_topic, voice_topic, frame_queue, voice_queue)

    def _on_connect(client, userdata, connect_flags, reason_code, properties) -> None:
        logger.debug("beacon: mqtt connected (reason_code=%s), subscribing", reason_code)
        client.subscribe([(frame_topic, BEACON_MQ_QOS), (voice_topic, BEACON_MQ_QOS)])

    client.on_connect = _on_connect

    while not stop_event.is_set():
        try:
            client.connect(BEACON_MQ_HOST, BEACON_MQ_PORT)
            break
        except Exception:
            logger.error(
                "beacon: mqtt failed to connect to %s:%d, retrying in %ds",
                BEACON_MQ_HOST, BEACON_MQ_PORT, BEACON_MQ_RECONNECT_BACKOFF_SECONDS,
                exc_info=True,
            )
            stop_event.wait(BEACON_MQ_RECONNECT_BACKOFF_SECONDS)
    else:
        logger.info("beacon: mqtt stopped before connecting")
        return

    client.loop_start()
    stop_event.wait()
    client.loop_stop()
    client.disconnect()
    logger.info("beacon: mqtt stopped")


# --- TDMA loop ---


def _load_window_config(conn) -> schedule.WindowConfig | None:
    """Re-read every tick — live-editable, no restart needed. None (with
    an error logged) on an invalid combination, e.g. voice+guard+frame >
    total; the caller skips this tick's transmission logic rather than
    crashing the whole process over one bad edit."""
    try:
        return schedule.WindowConfig(
            total_seconds=int(get_setting("BEACON_WINDOW_TOTAL_SECONDS", "90", conn=conn)),
            voice_seconds=int(get_setting("BEACON_WINDOW_VOICE_SECONDS", "60", conn=conn)),
            frame_seconds=int(get_setting("BEACON_WINDOW_FRAME_SECONDS", "30", conn=conn)),
            guard_seconds=int(get_setting("BEACON_WINDOW_GUARD_SECONDS", "0", conn=conn)),
        )
    except ValueError as exc:
        logger.error("beacon: invalid window config, skipping this tick: %s", exc)
        return None


def _seconds_until_slot_start(config: schedule.WindowConfig, slot: schedule.Slot, now: float) -> float:
    """How many seconds from now until `slot` (VOICE or FRAME) next
    begins. Measured against the slot's own start boundary, not against
    whatever slot happens to precede it — guard_seconds=0 (the default)
    collapses voice straight into frame with no gap slot to catch a
    transition in, so lead time must anchor to the target slot's start,
    not to "we just left the previous slot"."""
    _cycle_index, elapsed = divmod(now, config.total_seconds)
    target_start = 0.0 if slot is schedule.Slot.VOICE else float(config.voice_seconds + config.guard_seconds)
    if elapsed <= target_start:
        return target_start - elapsed
    return config.total_seconds - elapsed + target_start


def _switch_service(conn, service_controller: service_control.ServiceController, *, stop_name: str, start_name: str) -> None:
    stop_ok = service_controller.stop(stop_name)
    record_audit_event(
        conn,
        event_type="beacon.service.stopped" if stop_ok else "beacon.service.control_failed",
        actor="beacon",
        details={"service": stop_name, "action": "stop"},
    )
    start_ok = service_controller.start(start_name)
    record_audit_event(
        conn,
        event_type="beacon.service.started" if start_ok else "beacon.service.control_failed",
        actor="beacon",
        details={"service": start_name, "action": "start"},
    )


def _maybe_control_services(
    conn, config: schedule.WindowConfig, now: float,
    voice_queue: BoundedDropOldestQueue, frame_queue: BoundedDropOldestQueue,
    service_controller: service_control.ServiceController, svxlink_name: str, direwolf_name: str,
    lead_time: float, prepped_voice: bool, prepped_frame: bool,
) -> tuple[bool, bool]:
    """Stops/starts services ahead of each content slot's start, but only
    when there's actually something queued for it (avoids needless
    service restarts every cycle while idle). prepped_voice/prepped_frame
    are per-occurrence latches: set once triggered, reset back to False
    once we're outside the lead-time window again (which happens
    automatically once the target slot's own eta jumps forward to "next
    cycle" territory right as we enter it) — so each occurrence triggers
    exactly once."""
    voice_eta = _seconds_until_slot_start(config, schedule.Slot.VOICE, now)
    frame_eta = _seconds_until_slot_start(config, schedule.Slot.FRAME, now)

    if voice_eta <= lead_time:
        if not prepped_voice and voice_queue.stats().size > 0:
            _switch_service(conn, service_controller, stop_name=direwolf_name, start_name=svxlink_name)
            prepped_voice = True
    else:
        prepped_voice = False

    if frame_eta <= lead_time:
        if not prepped_frame and frame_queue.stats().size > 0:
            _switch_service(conn, service_controller, stop_name=svxlink_name, start_name=direwolf_name)
            prepped_frame = True
    else:
        prepped_frame = False

    return prepped_voice, prepped_frame


def _write_heartbeat(conn, state: schedule.SlotState, voice_stats, frame_stats) -> None:
    set_beacon_status(conn, "process_heartbeat_at", utc_now().isoformat())
    set_beacon_status(conn, "current_slot", state.slot.value)
    set_beacon_status(conn, "current_cycle_index", str(state.cycle_index))
    set_beacon_status(conn, "voice_queue_depth", str(voice_stats.size))
    set_beacon_status(conn, "voice_queue_dropped_total", str(voice_stats.dropped_total))
    set_beacon_status(conn, "frame_queue_depth", str(frame_stats.size))
    set_beacon_status(conn, "frame_queue_dropped_total", str(frame_stats.dropped_total))


def _run_ntp_check(conn) -> None:
    server = get_setting("BEACON_NTP_SERVER", "pool.ntp.org", conn=conn)
    max_offset = float(get_setting("BEACON_NTP_MAX_OFFSET_SECONDS", "2.0", conn=conn))
    result = ntp.check_offset(server)
    if result.ok and result.offset_seconds is not None:
        set_beacon_status(conn, "last_ntp_offset_seconds", str(result.offset_seconds))
        set_beacon_status(
            conn, "last_ntp_checked_at",
            datetime.fromtimestamp(result.checked_at, tz=timezone.utc).isoformat(),
        )
        if abs(result.offset_seconds) > max_offset:
            logger.error(
                "beacon: NTP offset %.3fs exceeds max allowed %.3fs (server=%s)",
                result.offset_seconds, max_offset, server,
            )
    record_audit_event(
        conn, event_type="beacon.ntp.checked", actor="beacon",
        details={
            "ok": result.ok, "offset_seconds": result.offset_seconds,
            "method": result.method, "error": result.error,
        },
    )


def _build_voice_transmitter(conn) -> voice.VoiceTransmitter:
    kind = get_setting("BEACON_VOICE_TRANSMITTER", "logging", conn=conn)
    if kind == "svxlink":
        return voice.SvxlinkControlTransmitter()
    return voice.LoggingVoiceTransmitter()


def _build_service_controller(conn) -> service_control.ServiceController:
    kind = get_setting("BEACON_SERVICE_CONTROLLER", "logging", conn=conn)
    if kind == "systemctl":
        return service_control.SystemctlServiceController()
    return service_control.LoggingServiceController()


def _try_transmit_voice(
    conn, voice_queue: BoundedDropOldestQueue, voice_transmitter: voice.VoiceTransmitter,
    callsign: str | None, template: str, max_chars: int, wav_dir: str, tts_voice: str,
) -> None:
    queued = voice_queue.get_nowait()
    if queued is None:
        return
    if not callsign:
        logger.warning("beacon: BEACON_CALLSIGN not configured, skipping voice transmission")
        record_audit_event(
            conn, event_type="beacon.voice.skipped_no_callsign", actor="beacon",
            source=queued.source, item_id=queued.item_id,
        )
        return

    text = content.resolve_voice_text(conn, queued.source, queued.item_id)
    if not text:
        logger.info(
            "beacon: source=%s item_id=%s has no resolvable voice text, skipping",
            queued.source, queued.item_id,
        )
        return

    formatted = formatters.format_voice(text, callsign=callsign, template=template, max_chars=max_chars)
    wav_path = Path(wav_dir) / f"{queued.source}-{queued.item_id}-{int(time.time())}.wav"
    if not voice.synthesize_speech(formatted.text, out_path=wav_path, voice=tts_voice):
        record_audit_event(
            conn, event_type="beacon.voice.transmit_failed", actor="beacon",
            source=queued.source, item_id=queued.item_id, details={"reason": "tts_failed"},
        )
        return

    try:
        sent = voice_transmitter.transmit(text=formatted.text, wav_path=wav_path)
    except Exception:
        logger.error("beacon: voice transmitter raised", exc_info=True)
        sent = False

    if sent:
        record_audit_event(
            conn, event_type="beacon.voice.transmitted", actor="beacon",
            source=queued.source, item_id=queued.item_id, details={"truncated": formatted.truncated},
        )
        set_beacon_status(conn, "last_voice_transmit_at", utc_now().isoformat())
    else:
        record_audit_event(
            conn, event_type="beacon.voice.transmit_failed", actor="beacon",
            source=queued.source, item_id=queued.item_id,
        )


def _try_transmit_frame(conn, frame_queue: BoundedDropOldestQueue, kiss_client: kiss.KissTcpClient, callsign: str | None, destination: str) -> None:
    queued = frame_queue.get_nowait()
    if queued is None:
        return
    if not callsign:
        logger.warning("beacon: BEACON_CALLSIGN not configured, skipping frame transmission")
        record_audit_event(
            conn, event_type="beacon.frame.skipped_no_callsign", actor="beacon",
            source=queued.source, item_id=queued.item_id,
        )
        return

    chunk_text = content.resolve_frame_text(conn, queued.source, queued.item_id, queued.chunk_index)
    if chunk_text is None:
        logger.info(
            "beacon: source=%s item_id=%s chunk_index=%d no longer exists, skipping",
            queued.source, queued.item_id, queued.chunk_index,
        )
        return

    try:
        formatted = formatters.format_frame(chunk_text, callsign=callsign, destination=destination)
    except formatters.FrameTooLongError as exc:
        logger.error("beacon: frame too long, dropping: %s", exc)
        record_audit_event(
            conn, event_type="beacon.frame.dropped_too_long", actor="beacon",
            source=queued.source, item_id=queued.item_id, details={"error": str(exc)},
        )
        return

    sent = kiss_client.send_ui_frame(
        source_callsign=callsign, dest_callsign=destination, info=chunk_text.encode("utf-8")
    )
    if sent:
        record_audit_event(
            conn, event_type="beacon.frame.transmitted", actor="beacon",
            source=queued.source, item_id=queued.item_id,
            details={"tnc2": formatted.tnc2, "byte_length": formatted.byte_length},
        )
        set_beacon_status(conn, "last_frame_transmit_at", utc_now().isoformat())
    else:
        record_audit_event(
            conn, event_type="beacon.frame.transmit_failed", actor="beacon",
            source=queued.source, item_id=queued.item_id,
        )


def _run_tdma_loop(stop_event: threading.Event, voice_queue: BoundedDropOldestQueue, frame_queue: BoundedDropOldestQueue) -> None:
    conn = get_connection(DEFAULT_DB_PATH)
    kiss_client: kiss.KissTcpClient | None = None
    try:
        set_beacon_status(conn, "process_started_at", utc_now().isoformat())

        kiss_host = get_setting("BEACON_AX25_KISS_HOST", "localhost", conn=conn)
        kiss_port = int(get_setting("BEACON_AX25_KISS_PORT", "8001", conn=conn))
        kiss_timeout = float(get_setting("BEACON_AX25_CONNECT_TIMEOUT_SECONDS", "5", conn=conn))
        kiss_client = kiss.KissTcpClient(kiss_host, kiss_port, connect_timeout=kiss_timeout)

        voice_transmitter = _build_voice_transmitter(conn)
        service_controller = _build_service_controller(conn)
        svxlink_name = get_setting("BEACON_SVXLINK_SERVICE_NAME", "svxlink", conn=conn)
        direwolf_name = get_setting("BEACON_DIREWOLF_SERVICE_NAME", "direwolf", conn=conn)
        wav_dir = get_setting("BEACON_TTS_WAV_DIR", "storage/beacon_tts", conn=conn)
        tts_voice = get_setting("BEACON_TTS_VOICE", "es", conn=conn)
        destination = get_setting("BEACON_FRAME_DESTINATION", "WXALRT", conn=conn)

        last_voice_cycle: int | None = None
        last_frame_cycle: int | None = None
        prepped_voice = False
        prepped_frame = False
        last_ntp_check_at = 0.0

        logger.info(
            "beacon: TDMA loop starting (kiss=%s:%d, voice_transmitter=%s, service_controller=%s)",
            kiss_host, kiss_port, type(voice_transmitter).__name__, type(service_controller).__name__,
        )

        while not stop_event.is_set():
            now = time.time()
            tick_seconds = int(get_setting("BEACON_TICK_SECONDS", "1", conn=conn))
            window = _load_window_config(conn)
            if window is None:
                stop_event.wait(tick_seconds)
                continue

            enabled = get_setting("BEACON_ENABLED", "false", conn=conn).lower() == "true"
            ntp_interval = int(get_setting("BEACON_NTP_CHECK_INTERVAL_SECONDS", "3600", conn=conn))
            lead_time = float(get_setting("BEACON_SLOT_LEAD_TIME_SECONDS", "2", conn=conn))
            callsign = get_setting("BEACON_CALLSIGN", conn=conn, env_fallback=False)
            voice_template = get_setting("BEACON_VOICE_TEMPLATE", "{callsign}. {text}", conn=conn)
            voice_max_chars = int(get_setting("ACTIONS_AI_MAX_CHARS", "200", conn=conn))

            if now - last_ntp_check_at >= ntp_interval:
                _run_ntp_check(conn)
                last_ntp_check_at = now

            state = schedule.current_slot(window, now)
            _write_heartbeat(conn, state, voice_queue.stats(), frame_queue.stats())

            prepped_voice, prepped_frame = _maybe_control_services(
                conn, window, now, voice_queue, frame_queue, service_controller,
                svxlink_name, direwolf_name, lead_time, prepped_voice, prepped_frame,
            )

            if enabled and state.slot is schedule.Slot.VOICE and state.cycle_index != last_voice_cycle:
                _try_transmit_voice(
                    conn, voice_queue, voice_transmitter, callsign, voice_template,
                    voice_max_chars, wav_dir, tts_voice,
                )
                last_voice_cycle = state.cycle_index
            elif enabled and state.slot is schedule.Slot.FRAME and state.cycle_index != last_frame_cycle:
                _try_transmit_frame(conn, frame_queue, kiss_client, callsign, destination)
                last_frame_cycle = state.cycle_index

            stop_event.wait(tick_seconds)
    finally:
        if kiss_client is not None:
            kiss_client.close()
        conn.close()
    logger.info("beacon: TDMA loop stopped")


def main() -> None:
    conn = get_connection(DEFAULT_DB_PATH)
    frame_topic = get_setting("BEACON_FRAME_SUBSCRIBE_TOPIC", "radiobeacon/events/item.chunked", conn=conn)
    voice_topic = get_setting("BEACON_VOICE_SUBSCRIBE_TOPIC", "radiobeacon/events/item.dispatched", conn=conn)
    queue_max_size = int(get_setting("BEACON_QUEUE_MAX_SIZE", "20", conn=conn))
    conn.close()

    voice_queue = BoundedDropOldestQueue(queue_max_size)
    frame_queue = BoundedDropOldestQueue(queue_max_size)

    stop_event = threading.Event()

    def _handle_shutdown_signal(signum, frame) -> None:
        logger.info("received signal %d, shutting down", signum)
        stop_event.set()

    signal.signal(signal.SIGINT, _handle_shutdown_signal)
    signal.signal(signal.SIGTERM, _handle_shutdown_signal)

    mqtt_thread = threading.Thread(
        target=_run_mqtt_client,
        args=(frame_topic, voice_topic, frame_queue, voice_queue, stop_event),
        name="beacon-mqtt",
        daemon=True,
    )
    tdma_thread = threading.Thread(
        target=_run_tdma_loop,
        args=(stop_event, voice_queue, frame_queue),
        name="beacon-tdma",
        daemon=True,
    )

    mqtt_thread.start()
    tdma_thread.start()

    mqtt_thread.join()
    tdma_thread.join()


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)-8s %(name)s: %(message)s"
    )
    main()
