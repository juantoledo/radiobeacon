"""The TDMA transmission loop — beacon's entry point.

Content flows in from a single MQTT subscription: item.content_ready
(actions.content_ready), published only once BOTH actions.chunk and
actions.ai have finished reacting to the same dispatch — a race-free
"everything that was going to happen to this item's content has happened"
signal, replacing what used to be two independent subscriptions
(item.chunked for frame, item.dispatched for voice) that raced against
each other and left AX.25 frames carrying raw chunked text even when an
AI summary existed. Since then, actions.chunk was changed to subscribe to
actions.ai's own output rather than item.dispatched directly — so chunk
now always runs AFTER ai has settled, chunking the AI summary when one
exists and falling back to extracted_contents otherwise (see chunk.py) —
meaning the `chunks` table already reflects the best available content
by the time content_ready fires. Frame enqueue is therefore simple: one
QueuedFrame per existing chunks row, always — no special-casing needed
here for whether a summary existed. Voice always gets one QueuedVoice —
its own resolve_voice_text independently prefers items.summary, falling
back to extracted_contents, at transmit time. See content.py for how the
actual text gets resolved (lazily, at transmit time, not baked in at
enqueue time) and formatters.py for why length limits reuse
ACTIONS_CHUNK_MAX_CHARS/ACTIONS_AI_MAX_CHARS instead of new
beacon-specific settings.

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
from adapters.timeutil import to_display_tz, utc_now  # noqa: E402

from beacon import content, formatters, kiss, mq, ntp, schedule, service_control, voice  # noqa: E402
from beacon.queues import BoundedDropOldestQueue  # noqa: E402

logger = logging.getLogger(__name__)

BEACON_MQ_HOST = get_setting("BEACON_MQ_HOST", "localhost")
BEACON_MQ_PORT = int(get_setting("BEACON_MQ_PORT", "1883"))
BEACON_MQ_QOS = int(get_setting("BEACON_MQ_QOS", "1"))
BEACON_MQ_RECONNECT_BACKOFF_SECONDS = int(get_setting("BEACON_MQ_RECONNECT_BACKOFF_SECONDS", "5"))


# --- MQTT ingest: one subscription feeding both queues ---


def _already_enqueued(conn, event_id: str | None) -> bool:
    """Same event-id-keyed idempotency as actions/__main__.py's
    _already_processed (and the same fix applied there) — a rearm
    publishes a genuinely new CloudEvent for the same (source, item_id),
    which must be enqueued again, not silently skipped."""
    if not event_id:
        return False
    row = conn.execute(
        "SELECT 1 FROM audit_log WHERE event_type = 'beacon.content_ready.enqueued' "
        "AND json_extract(details, '$.event_id') = ? LIMIT 1",
        (event_id,),
    ).fetchone()
    return row is not None


def _handle_content_ready_event(
    conn, frame_queue: BoundedDropOldestQueue, voice_queue: BoundedDropOldestQueue,
    source: str, item_id: str, event_id: str | None,
) -> None:
    if _already_enqueued(conn, event_id):
        logger.info("beacon: content_ready event_id=%s already enqueued, skipping", event_id)
        return

    frame_count = 0
    rows = conn.execute(
        "SELECT chunk_index FROM chunks WHERE source = ? AND item_id = ? ORDER BY chunk_index",
        (source, item_id),
    ).fetchall()
    for (chunk_index,) in rows:
        ok = frame_queue.put(content.QueuedFrame(source=source, item_id=item_id, chunk_index=chunk_index))
        if ok:
            frame_count += 1
        else:
            record_audit_event(
                conn, event_type="beacon.queue.dropped", actor="beacon",
                source=source, item_id=item_id, details={"queue": "frame"},
            )

    voice_ok = voice_queue.put(content.QueuedVoice(source=source, item_id=item_id))
    if not voice_ok:
        record_audit_event(
            conn, event_type="beacon.queue.dropped", actor="beacon",
            source=source, item_id=item_id, details={"queue": "voice"},
        )

    record_audit_event(
        conn, event_type="beacon.content_ready.enqueued", actor="beacon", source=source, item_id=item_id,
        details={"event_id": event_id, "frame_count": frame_count, "voice_enqueued": voice_ok},
    )


def _make_on_message(content_ready_topic: str, frame_queue: BoundedDropOldestQueue, voice_queue: BoundedDropOldestQueue):
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
                if message.topic == content_ready_topic:
                    _handle_content_ready_event(conn, frame_queue, voice_queue, source, item_id, event_id)
                else:
                    logger.warning("beacon: message on unexpected topic %s", message.topic)
            finally:
                conn.close()
        except Exception:
            logger.error("beacon: failed handling message on %s", message.topic, exc_info=True)

    return _handler


def _run_mqtt_client(content_ready_topic: str, frame_queue: BoundedDropOldestQueue, voice_queue: BoundedDropOldestQueue, stop_event: threading.Event) -> None:
    """Mirrors actions/__main__.py's _run_action_loop: loop_start() +
    stop_event.wait() (not loop_forever(), which can't be signaled from a
    threading.Event), stable client_id + clean_session=True, resubscribe
    on every (re)connect.

    clean_session=True (flipped from False this session, matching the
    same fix in actions/__main__.py): MQTT SUBSCRIBE is purely additive,
    so a persistent session across a topic rename (beacon's own
    subscription collapsing from item.chunked + item.dispatched to a
    single item.content_ready earlier this session) kept BOTH old
    subscriptions alive forever alongside the new one — confirmed
    directly in the broker's own persistence file. Harmless here only by
    luck (_make_on_message already ignores an unrecognized topic), but
    the same bug on actions.chunk's equivalent stale subscription caused
    real double-transmission — fixed the same way everywhere rather than
    relying on this one handler's defensive check. A message published
    while beacon is briefly offline is now lost rather than queued —
    recoverable via a rearm."""
    import paho.mqtt.client as mqtt_client

    logger.info("beacon: mqtt starting (content_ready_topic=%s)", content_ready_topic)

    client = mqtt_client.Client(
        mqtt_client.CallbackAPIVersion.VERSION2,
        client_id="radiobeacon-beacon",
        clean_session=True,
    )
    client.on_message = _make_on_message(content_ready_topic, frame_queue, voice_queue)

    def _on_connect(client, userdata, connect_flags, reason_code, properties) -> None:
        logger.debug("beacon: mqtt connected (reason_code=%s), subscribing", reason_code)
        client.subscribe([(content_ready_topic, BEACON_MQ_QOS)])

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
    # Straight from SlotState, already computed every tick by schedule.py —
    # the UI (ui/src/ui/routers/beacon.py) uses these for a live cycle
    # timeline + countdown, rather than re-deriving scheduling logic itself.
    set_beacon_status(conn, "current_cycle_elapsed_seconds", str(round(state.elapsed_in_cycle, 1)))
    set_beacon_status(conn, "current_slot_remaining_seconds", str(round(state.remaining_in_slot, 1)))
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


def _format_source_date_time(conn, source: str, item_id: str, date_format: str) -> str:
    """Resolves items.source_date_time (if any), converts UTC -> the
    configured DISPLAY_TIMEZONE (this is presentation, exactly the case
    adapters.timeutil.to_display_tz exists for), and renders it via
    date_format (BEACON_DATE_FORMAT). "" (not None) when there's no
    source_date_time -- so a template referencing {date} renders a blank
    rather than crashing."""
    dt = content.resolve_source_date_time(conn, source, item_id)
    if dt is None:
        return ""
    return to_display_tz(dt).strftime(date_format)


def _try_transmit_voice(
    conn, voice_queue: BoundedDropOldestQueue, voice_transmitter: voice.VoiceTransmitter,
    callsign: str | None, template: str, max_chars: int, wav_dir: str, tts_voice: str, date_format: str,
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

    date_str = _format_source_date_time(conn, queued.source, queued.item_id, date_format)
    formatted = formatters.format_voice(
        text, callsign=callsign, template=template, max_chars=max_chars, date=date_str
    )
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


def _try_transmit_frame(
    conn, frame_queue: BoundedDropOldestQueue, kiss_client: kiss.KissTcpClient,
    callsign: str | None, destination: str, prefix: str = "", suffix: str = "", date_format: str = "",
) -> None:
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
            "beacon: source=%s item_id=%s chunk_index=%s no longer resolvable, skipping",
            queued.source, queued.item_id, queued.chunk_index,
        )
        return

    date_str = _format_source_date_time(conn, queued.source, queued.item_id, date_format)
    try:
        formatted = formatters.format_frame(
            chunk_text, callsign=callsign, destination=destination,
            prefix=prefix, suffix=suffix, date=date_str,
        )
    except formatters.FrameTooLongError as exc:
        logger.error("beacon: frame too long, dropping: %s", exc)
        record_audit_event(
            conn, event_type="beacon.frame.dropped_too_long", actor="beacon",
            source=queued.source, item_id=queued.item_id, details={"error": str(exc)},
        )
        return

    sent = kiss_client.send_ui_frame(
        source_callsign=callsign, dest_callsign=destination, info=formatted.content.encode("utf-8")
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


def _drain_and_transmit(
    stop_event: threading.Event, queue: BoundedDropOldestQueue, inter_tx_delay: float, transmit_once,
) -> int:
    """Repeatedly calls transmit_once() (each call already does one
    queue.get_nowait() + transmit + audit-record) while `queue` is
    non-empty, instead of attempting only once per slot occurrence — the
    slot's nominal length is a floor, not a hard ceiling: this keeps
    going even past it until the backlog is empty, pausing
    inter_tx_delay between transmissions (not before the first, not
    after the last) so real hardware gets a beat for PTT release/re-key
    between them. See beacon/README.md's TDMA section for the tradeoff
    this implies (blocks _maybe_control_services/NTP/heartbeat for the
    drain's duration — bounded by BEACON_QUEUE_MAX_SIZE).
    stop_event.is_set() is checked so shutdown stays responsive instead
    of forcing a full backlog to flush first. Returns the number of
    items attempted (mainly for tests)."""
    attempted = 0
    while not stop_event.is_set() and queue.stats().size > 0:
        if attempted > 0:
            stop_event.wait(inter_tx_delay)
        transmit_once()
        attempted += 1
    return attempted


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
        frame_prefix = get_setting("BEACON_FRAME_PREFIX", "", conn=conn) or ""
        frame_suffix = get_setting("BEACON_FRAME_SUFFIX", "", conn=conn) or ""

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
            voice_template = get_setting("BEACON_VOICE_TEMPLATE", "{callsign}. {text}. {date}", conn=conn)
            voice_max_chars = int(get_setting("ACTIONS_AI_MAX_CHARS", "200", conn=conn))
            date_format = get_setting("BEACON_DATE_FORMAT", "%d-%m-%Y %H:%M", conn=conn)
            voice_inter_tx_delay = float(get_setting("BEACON_VOICE_INTER_TX_DELAY_SECONDS", "2", conn=conn))
            frame_inter_tx_delay = float(get_setting("BEACON_FRAME_INTER_TX_DELAY_SECONDS", "2", conn=conn))

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
                _drain_and_transmit(
                    stop_event, voice_queue, voice_inter_tx_delay,
                    lambda: _try_transmit_voice(
                        conn, voice_queue, voice_transmitter, callsign, voice_template,
                        voice_max_chars, wav_dir, tts_voice, date_format,
                    ),
                )
                last_voice_cycle = state.cycle_index
            elif enabled and state.slot is schedule.Slot.FRAME and state.cycle_index != last_frame_cycle:
                _drain_and_transmit(
                    stop_event, frame_queue, frame_inter_tx_delay,
                    lambda: _try_transmit_frame(
                        conn, frame_queue, kiss_client, callsign, destination,
                        frame_prefix, frame_suffix, date_format,
                    ),
                )
                last_frame_cycle = state.cycle_index

            stop_event.wait(tick_seconds)
    finally:
        if kiss_client is not None:
            kiss_client.close()
        conn.close()
    logger.info("beacon: TDMA loop stopped")


def main() -> None:
    conn = get_connection(DEFAULT_DB_PATH)
    content_ready_topic = get_setting(
        "BEACON_CONTENT_READY_SUBSCRIBE_TOPIC", "radiobeacon/events/item.content_ready", conn=conn
    )
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
        args=(content_ready_topic, frame_queue, voice_queue, stop_event),
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
