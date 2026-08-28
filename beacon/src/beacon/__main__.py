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
its own resolve_voice_text reads items.summary directly at transmit
time, with no fallback of its own: actions.ai now guarantees summary is
always populated (a real summary, or extracted_contents copied in
verbatim) by the time content_ready fires. See content.py for how the
actual text gets resolved (lazily, at transmit time, not baked in at
enqueue time) and formatters.py for how each channel's length limit is
sourced: frame reuses actions.chunk's ACTIONS_CHUNK_MAX_CHARS, while voice
has its own dedicated BEACON_VOICE_MAX_CHARS — deliberately NOT
ACTIONS_AI_MAX_CHARS, whose job is gating whether actions.ai's LLM call
runs at all, not bounding how much of a (now always-populated) summary
voice actually speaks. Each channel also has its own prefix/suffix
(BEACON_FRAME_PREFIX/SUFFIX, BEACON_VOICE_PREFIX/SUFFIX) wrapping its
content, plus voice's outer BEACON_VOICE_TEMPLATE — all three str.format
templates, sharing one placeholder vocabulary beyond {date}: {source},
{item_id}, {type}, {subtype}, {extracted_title}, {url}, {source_name},
{source_url} (see content.resolve_item_fields, resolved fresh per item at
transmit time, same as the text itself). {source_name}/{source_url} are
per-SOURCE, not per-item — a display name and general site URL from the
`sources` table (adapters.storage — seeded with csn/senapred, editable
live via data-adapters/sources.sh), distinct from {source} (the raw
internal key) and {url} (this specific item's own link, e.g. a per-alert
SENAPRED URL).

BEACON_ENABLED (decision: soft enable/disable, not real process control —
see beacon/README.md) is re-read every tick; enqueueing from MQTT happens
unconditionally regardless of it (the "strict queue" framing) — only the
dequeue+transmit step checks it. This is also what "restart from the UI"
means in practice: the next tick just re-reads settings, no process
kill/respawn involved.

The TDMA loop's wait between ticks is a wake_event.wait(timeout=
tick_seconds), not a plain sleep -- _handle_content_ready_event sets
wake_event right after a successful queue.put() (both the live MQTT path
and _reconcile_missed_content_ready's catch-up path), so a newly-queued
item is picked up on the very next loop iteration rather than waiting out
the rest of BEACON_TICK_SECONDS. Draining is no longer gated to "once per
slot occurrence" either -- _drain_and_transmit is attempted every
iteration the current slot matches (a cheap no-op when its queue is
empty), so an item queued midway through an already-active slot is
transmitted within that same occurrence, not deferred to the slot's next
cycle. Once a transmission has started it always runs to completion even
past the slot's nominal end (see _drain_and_transmit's own docstring) --
this wake-on-enqueue change doesn't alter that, since draining is still
one synchronous, blocking call on this same thread; it only removes the
before-hand delay in noticing new content, not the "floor, not a hard
ceiling" guarantee once it starts.

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

from adapters.beacon_defaults import (  # noqa: E402
    BEACON_ENABLED_DEFAULT,
    BEACON_QUEUE_MAX_SIZE_DEFAULT,
    BEACON_WINDOW_FRAME_SECONDS_DEFAULT,
    BEACON_WINDOW_GUARD_SECONDS_DEFAULT,
    BEACON_WINDOW_TOTAL_SECONDS_DEFAULT,
    BEACON_WINDOW_VOICE_SECONDS_DEFAULT,
)
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
    wake_event: threading.Event | None = None,
) -> None:
    """wake_event (optional -- None from _reconcile_missed_content_ready,
    which already runs on the TDMA thread itself and has no need to wake
    it) is set after enqueueing so the TDMA loop's tick-wait returns
    immediately instead of sleeping up to BEACON_TICK_SECONDS -- see
    _run_tdma_loop."""
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

    if wake_event is not None:
        wake_event.set()

    record_audit_event(
        conn, event_type="beacon.content_ready.enqueued", actor="beacon", source=source, item_id=item_id,
        details={"event_id": event_id, "frame_count": frame_count, "voice_enqueued": voice_ok},
    )


def _reconcile_missed_content_ready(conn, frame_queue: BoundedDropOldestQueue, voice_queue: BoundedDropOldestQueue) -> int:
    """Catches up on any item.content_ready publish beacon's own MQTT
    subscription genuinely missed — regardless of why: this process
    starting up after actions.content_ready already published a backlog
    (confirmed live: 52 publishes in one burst, all lost, because
    beacon's client hadn't finished connecting/subscribing yet), a broker
    restart, or any other brief disconnect. clean_session=True (see
    _run_mqtt_client's docstring for why) trades away broker-side
    queueing for a disconnected client, so MQTT delivery alone can no
    longer be trusted as the sole source of truth here.

    Compares item_readiness (actions.content_ready's own durable "I
    published for this item" record — see data-adapters/src/adapters/
    storage.py) against beacon's own beacon.content_ready.enqueued audit
    trail: any item whose latest content-ready publish is newer than (or
    has no matching) enqueue gets enqueued now, via the exact same
    _handle_content_ready_event path a live MQTT message would take
    (event_id=None — _already_enqueued always treats a missing event_id
    as "not yet seen", and the resulting audit row's own timestamp is
    what closes the gap for next time, so this is naturally
    self-correcting, no separate bookkeeping needed). Returns the count
    reconciled (for logging)."""
    rows = conn.execute(
        """
        SELECT r.source, r.item_id
        FROM item_readiness r
        LEFT JOIN (
            SELECT source, item_id, MAX(recorded_at) AS last_enqueued
            FROM audit_log
            WHERE event_type = 'beacon.content_ready.enqueued'
            GROUP BY source, item_id
        ) e ON e.source = r.source AND e.item_id = r.item_id
        WHERE e.last_enqueued IS NULL OR r.published_at > e.last_enqueued
        """
    ).fetchall()
    for source, item_id in rows:
        logger.warning(
            "beacon: reconciling missed item.content_ready for source=%s item_id=%s "
            "(published but never enqueued — MQTT delivery was missed)",
            source, item_id,
        )
        _handle_content_ready_event(conn, frame_queue, voice_queue, source, item_id, None)
    return len(rows)


def _make_on_message(
    content_ready_topic: str, frame_queue: BoundedDropOldestQueue, voice_queue: BoundedDropOldestQueue,
    wake_event: threading.Event,
):
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
                    _handle_content_ready_event(
                        conn, frame_queue, voice_queue, source, item_id, event_id, wake_event
                    )
                else:
                    logger.warning("beacon: message on unexpected topic %s", message.topic)
            finally:
                conn.close()
        except Exception:
            logger.error("beacon: failed handling message on %s", message.topic, exc_info=True)

    return _handler


def _run_mqtt_client(
    content_ready_topic: str, frame_queue: BoundedDropOldestQueue, voice_queue: BoundedDropOldestQueue,
    stop_event: threading.Event, wake_event: threading.Event,
) -> None:
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
    while beacon is briefly offline is now lost at the MQTT layer — but
    unlike actions' own clients, beacon doesn't rely on that alone:
    _reconcile_missed_content_ready (see _run_tdma_loop) periodically
    catches up on anything genuinely missed via item_readiness/audit_log,
    confirmed live to close exactly this gap (52 item.content_ready
    publishes lost to this startup race in one incident before this
    existed)."""
    import paho.mqtt.client as mqtt_client

    logger.info("beacon: mqtt starting (content_ready_topic=%s)", content_ready_topic)

    client = mqtt_client.Client(
        mqtt_client.CallbackAPIVersion.VERSION2,
        client_id="radiobeacon-beacon",
        clean_session=True,
    )
    client.on_message = _make_on_message(content_ready_topic, frame_queue, voice_queue, wake_event)

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
            total_seconds=int(get_setting("BEACON_WINDOW_TOTAL_SECONDS", BEACON_WINDOW_TOTAL_SECONDS_DEFAULT, conn=conn)),
            voice_seconds=int(get_setting("BEACON_WINDOW_VOICE_SECONDS", BEACON_WINDOW_VOICE_SECONDS_DEFAULT, conn=conn)),
            frame_seconds=int(get_setting("BEACON_WINDOW_FRAME_SECONDS", BEACON_WINDOW_FRAME_SECONDS_DEFAULT, conn=conn)),
            guard_seconds=int(get_setting("BEACON_WINDOW_GUARD_SECONDS", BEACON_WINDOW_GUARD_SECONDS_DEFAULT, conn=conn)),
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
    return to_display_tz(dt, conn=conn).strftime(date_format)


def _try_transmit_voice(
    conn, voice_queue: BoundedDropOldestQueue, voice_transmitter: voice.VoiceTransmitter,
    callsign: str | None, template: str, max_chars: int, wav_dir: str, tts_voice: str, date_format: str,
    tts_engine: str = "espeak", tts_piper_model: str = "", tts_piper_binary: str = "piper",
    prefix: str = "", suffix: str = "",
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
    item_fields = content.resolve_item_fields(conn, queued.source, queued.item_id)
    item_fields.update(source=queued.source, item_id=queued.item_id)
    formatted = formatters.format_voice(
        text, callsign=callsign, template=template, max_chars=max_chars,
        prefix=prefix, suffix=suffix, date=date_str, **item_fields,
    )
    wav_path = Path(wav_dir) / f"{queued.source}-{queued.item_id}-{int(time.time())}.wav"
    if not voice.synthesize_speech(
        formatted.text, out_path=wav_path, voice=tts_voice,
        engine=tts_engine, piper_model=tts_piper_model, piper_binary=tts_piper_binary,
    ):
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
    item_fields = content.resolve_item_fields(conn, queued.source, queued.item_id)
    item_fields.update(source=queued.source, item_id=queued.item_id)
    try:
        formatted = formatters.format_frame(
            chunk_text, callsign=callsign, destination=destination,
            prefix=prefix, suffix=suffix, date=date_str, **item_fields,
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


def _run_tdma_loop(
    stop_event: threading.Event, wake_event: threading.Event,
    voice_queue: BoundedDropOldestQueue, frame_queue: BoundedDropOldestQueue,
) -> None:
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
        tts_engine = get_setting("BEACON_TTS_ENGINE", "piper", conn=conn)
        tts_piper_model = get_setting(
            "BEACON_TTS_PIPER_MODEL", "storage/piper_voices/es_MX-claude-high.onnx", conn=conn
        )
        tts_piper_binary = get_setting("BEACON_TTS_PIPER_BINARY", "piper", conn=conn)

        prepped_voice = False
        prepped_frame = False
        last_ntp_check_at = 0.0
        # 0.0 so the very first tick immediately reconciles -- this is
        # what closes the startup race where actions.content_ready can
        # publish a backlog before beacon's own MQTT client has finished
        # connecting/subscribing (see _reconcile_missed_content_ready).
        last_reconcile_at = 0.0

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

            enabled = get_setting("BEACON_ENABLED", BEACON_ENABLED_DEFAULT, conn=conn).lower() == "true"
            ntp_interval = int(get_setting("BEACON_NTP_CHECK_INTERVAL_SECONDS", "3600", conn=conn))
            reconcile_interval = int(
                get_setting("BEACON_CONTENT_READY_RECONCILE_INTERVAL_SECONDS", "30", conn=conn)
            )
            lead_time = float(get_setting("BEACON_SLOT_LEAD_TIME_SECONDS", "2", conn=conn))
            callsign = get_setting("BEACON_CALLSIGN", conn=conn, env_fallback=False)
            voice_template = get_setting("BEACON_VOICE_TEMPLATE", "{callsign}. {text}. {date}", conn=conn)
            voice_max_chars = int(get_setting("BEACON_VOICE_MAX_CHARS", "500", conn=conn))
            date_format = get_setting("BEACON_DATE_FORMAT", "%d-%m-%Y %H:%M", conn=conn)
            voice_inter_tx_delay = float(get_setting("BEACON_VOICE_INTER_TX_DELAY_SECONDS", "2", conn=conn))
            frame_inter_tx_delay = float(get_setting("BEACON_FRAME_INTER_TX_DELAY_SECONDS", "2", conn=conn))
            # Moved here from the one-time setup block above (same bug
            # class as BEACON_QUEUE_MAX_SIZE, see set_maxsize's docstring)
            # -- a /config edit now takes effect on the very next tick.
            destination = get_setting("BEACON_FRAME_DESTINATION", "NFO", conn=conn)
            frame_prefix = get_setting("BEACON_FRAME_PREFIX", "", conn=conn) or ""
            frame_suffix = get_setting("BEACON_FRAME_SUFFIX", "", conn=conn) or ""
            voice_prefix = get_setting("BEACON_VOICE_PREFIX", "", conn=conn) or ""
            voice_suffix = get_setting("BEACON_VOICE_SUFFIX", "", conn=conn) or ""

            # Re-applied every tick (not just read once at process start
            # in main()) so a /config edit takes effect immediately,
            # matching every other beacon setting -- see queues.py's
            # set_maxsize docstring for why this one previously didn't.
            queue_max_size = int(get_setting("BEACON_QUEUE_MAX_SIZE", BEACON_QUEUE_MAX_SIZE_DEFAULT, conn=conn))
            voice_queue.set_maxsize(queue_max_size)
            frame_queue.set_maxsize(queue_max_size)

            if now - last_ntp_check_at >= ntp_interval:
                _run_ntp_check(conn)
                last_ntp_check_at = now

            if now - last_reconcile_at >= reconcile_interval:
                reconciled = _reconcile_missed_content_ready(conn, frame_queue, voice_queue)
                if reconciled:
                    logger.warning("beacon: reconciled %d missed item.content_ready publish(es)", reconciled)
                last_reconcile_at = now

            state = schedule.current_slot(window, now)
            _write_heartbeat(conn, state, voice_queue.stats(), frame_queue.stats())

            prepped_voice, prepped_frame = _maybe_control_services(
                conn, window, now, voice_queue, frame_queue, service_controller,
                svxlink_name, direwolf_name, lead_time, prepped_voice, prepped_frame,
            )

            if enabled and state.slot is schedule.Slot.VOICE:
                _drain_and_transmit(
                    stop_event, voice_queue, voice_inter_tx_delay,
                    lambda: _try_transmit_voice(
                        conn, voice_queue, voice_transmitter, callsign, voice_template,
                        voice_max_chars, wav_dir, tts_voice, date_format,
                        tts_engine, tts_piper_model, tts_piper_binary,
                        voice_prefix, voice_suffix,
                    ),
                )
            elif enabled and state.slot is schedule.Slot.FRAME:
                _drain_and_transmit(
                    stop_event, frame_queue, frame_inter_tx_delay,
                    lambda: _try_transmit_frame(
                        conn, frame_queue, kiss_client, callsign, destination,
                        frame_prefix, frame_suffix, date_format,
                    ),
                )

            # Woken immediately by wake_event.set() (queue.put() in
            # _handle_content_ready_event) rather than sleeping the full
            # tick_seconds -- an item queued mid-slot is picked up on the
            # very next iteration, not held until the next tick or (with
            # the removed cycle_index gate above) the next cycle. Cleared
            # right after waking so the next wait() blocks again instead
            # of spinning. Shutdown also sets wake_event (see
            # _handle_shutdown_signal), so this wakes just as fast as the
            # stop_event.wait() it replaces did.
            wake_event.wait(timeout=tick_seconds)
            wake_event.clear()
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
    queue_max_size = int(get_setting("BEACON_QUEUE_MAX_SIZE", BEACON_QUEUE_MAX_SIZE_DEFAULT, conn=conn))
    conn.close()

    voice_queue = BoundedDropOldestQueue(queue_max_size)
    frame_queue = BoundedDropOldestQueue(queue_max_size)

    stop_event = threading.Event()
    # Set alongside stop_event on shutdown, and by _handle_content_ready_event
    # after every successful enqueue -- lets _run_tdma_loop's tick-wait
    # return immediately instead of sleeping up to BEACON_TICK_SECONDS.
    wake_event = threading.Event()

    def _handle_shutdown_signal(signum, frame) -> None:
        logger.info("received signal %d, shutting down", signum)
        stop_event.set()
        wake_event.set()

    signal.signal(signal.SIGINT, _handle_shutdown_signal)
    signal.signal(signal.SIGTERM, _handle_shutdown_signal)

    mqtt_thread = threading.Thread(
        target=_run_mqtt_client,
        args=(content_ready_topic, frame_queue, voice_queue, stop_event, wake_event),
        name="beacon-mqtt",
        daemon=True,
    )
    tdma_thread = threading.Thread(
        target=_run_tdma_loop,
        args=(stop_event, wake_event, voice_queue, frame_queue),
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
