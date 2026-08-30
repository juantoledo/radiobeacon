"""The TDMA transmission loop — beacon's entry point.

Content flows in from a single MQTT subscription: item.content_ready
(actions.content_ready), published only once BOTH actions.chunk and
actions.ai have finished reacting to the same dispatch — a race-free
"everything that was going to happen to this item's content has happened"
signal. On each item.content_ready, beacon inserts one row per existing
`chunks` row (kind="frame") plus one row (kind="voice") into the durable
`beacon_tx_schedule` table (adapters.storage), snapshotting the item's
`transmit_policy` NAME. That table — not an in-memory queue — is the
source of truth for what still has to go on air and how many more times,
so a beacon restart no longer loses pending transmissions.

How often each row is transmitted, and how far apart, is resolved live
every cycle from `transmit_policy` via adapters.transmit_policy.policy_for
(repeat_times / interval_seconds) — so editing a tier in `transmit_policies`
(dispatcher/policies.sh or the ui's /policies) changes in-flight behavior
on the next cycle. A row is deleted once sent_count >= repeat_times. Every
transmission *attempt* counts against the budget (a run of failures still
retires the row), mirroring the old _try_transmit_* which recorded a
*_transmit_failed audit row and moved on.

The TDMA loop handles slot kinds generically via SLOT_KINDS /
KIND_TRANSMITTERS — adding a future repeatable slot type is a Slot value
plus two registry entries, no loop-body change.

See content.py for how the actual text gets resolved (lazily, at transmit
time, not baked in at schedule time) and formatters.py for how each
channel's length limit is sourced: frame reuses actions.chunk's
ACTIONS_CHUNK_MAX_CHARS, while voice has its own dedicated
BEACON_VOICE_MAX_CHARS. Each channel also has its own prefix/suffix
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
see beacon/README.md) is re-read every tick; scheduling rows from MQTT
happens unconditionally regardless of it — only the transmit step checks
it.

The TDMA loop's wait between ticks is a wake_event.wait(timeout=
tick_seconds), not a plain sleep -- _handle_content_ready_event sets
wake_event right after scheduling rows (both the live MQTT path and
_reconcile_missed_content_ready's catch-up path), so newly-scheduled
content is picked up on the very next loop iteration.

Audio-device contention between SvxLink and Direwolf is handled by
actively stopping/starting each service around its slot (see
service_control.py) — CONTEXT.md's strategy #1."""
import logging
import signal
import sys
import threading
import time
from datetime import datetime, timedelta, timezone
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
    add_tx_schedule_unit,
    count_tx_schedule_by_kind,
    due_tx_schedule_rows,
    get_connection,
    get_setting,
    has_pending_tx_schedule,
    record_audit_event,
    record_tx_schedule_sent,
    set_beacon_status,
)
from adapters.timeutil import to_display_tz, utc_now  # noqa: E402
from adapters.transmit_policy import policy_for  # noqa: E402

from beacon import content, formatters, kiss, mq, ntp, schedule, service_control, voice  # noqa: E402

logger = logging.getLogger(__name__)

BEACON_MQ_HOST = get_setting("BEACON_MQ_HOST", "localhost")
BEACON_MQ_PORT = int(get_setting("BEACON_MQ_PORT", "1883"))
BEACON_MQ_QOS = int(get_setting("BEACON_MQ_QOS", "1"))
BEACON_MQ_RECONNECT_BACKOFF_SECONDS = int(get_setting("BEACON_MQ_RECONNECT_BACKOFF_SECONDS", "5"))


# --- MQTT ingest: one subscription scheduling transmit rows ---


def _already_enqueued(conn, event_id: str | None) -> bool:
    """Same event-id-keyed idempotency as actions/__main__.py's
    _already_processed — a rearm publishes a genuinely new CloudEvent for
    the same (source, item_id), which must be scheduled again, not
    silently skipped."""
    if not event_id:
        return False
    row = conn.execute(
        "SELECT 1 FROM audit_log WHERE event_type = 'beacon.content_ready.enqueued' "
        "AND json_extract(details, '$.event_id') = ? LIMIT 1",
        (event_id,),
    ).fetchone()
    return row is not None


def _handle_content_ready_event(
    conn, source: str, item_id: str, event_id: str | None,
    wake_event: threading.Event | None = None,
) -> None:
    """Inserts/resets beacon_tx_schedule rows for an item: one kind="frame"
    row per existing `chunks` row, one kind="voice" row. wake_event
    (optional -- None from _reconcile_missed_content_ready) is set after
    scheduling so the TDMA loop's tick-wait returns immediately."""
    if _already_enqueued(conn, event_id):
        logger.info("beacon: content_ready event_id=%s already scheduled, skipping", event_id)
        return

    policy_row = conn.execute(
        "SELECT transmit_policy FROM items WHERE source = ? AND item_id = ?",
        (source, item_id),
    ).fetchone()
    transmit_policy = policy_row[0] if policy_row is not None else None

    max_size = int(get_setting("BEACON_QUEUE_MAX_SIZE", BEACON_QUEUE_MAX_SIZE_DEFAULT, conn=conn))

    rows = conn.execute(
        "SELECT chunk_index FROM chunks WHERE source = ? AND item_id = ? ORDER BY chunk_index",
        (source, item_id),
    ).fetchall()
    for (chunk_index,) in rows:
        add_tx_schedule_unit(
            conn, source, item_id, "frame", str(chunk_index),
            transmit_policy, event_id, max_size=max_size,
        )

    add_tx_schedule_unit(
        conn, source, item_id, "voice", "", transmit_policy, event_id, max_size=max_size
    )

    if wake_event is not None:
        wake_event.set()

    record_audit_event(
        conn, event_type="beacon.content_ready.enqueued", actor="beacon", source=source, item_id=item_id,
        details={"event_id": event_id, "frame_count": len(rows), "voice_enqueued": True},
    )


def _reconcile_missed_content_ready(conn, wake_event: threading.Event | None = None) -> int:
    """Catches up on any item.content_ready publish beacon's own MQTT
    subscription genuinely missed (startup race, broker restart, brief
    disconnect — clean_session=True trades away broker-side queueing).

    Compares item_readiness (actions.content_ready's own durable "I
    published for this item" record) against beacon's own
    beacon.content_ready.enqueued audit trail: any item whose latest
    content-ready publish is newer than (or has no matching) enqueue gets
    scheduled now, via the exact same _handle_content_ready_event path a
    live MQTT message would take (event_id=None). Returns the count
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
            "(published but never scheduled — MQTT delivery was missed)",
            source, item_id,
        )
        _handle_content_ready_event(conn, source, item_id, None, wake_event)
    return len(rows)


def _make_on_message(content_ready_topic: str, wake_event: threading.Event):
    """paho-mqtt re-raises any exception an on_message callback doesn't
    catch, silently killing loop_start()'s background thread — everything
    here is wrapped in one unconditional try/except."""

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
                    _handle_content_ready_event(conn, source, item_id, event_id, wake_event)
                else:
                    logger.warning("beacon: message on unexpected topic %s", message.topic)
            finally:
                conn.close()
        except Exception:
            logger.error("beacon: failed handling message on %s", message.topic, exc_info=True)

    return _handler


def _run_mqtt_client(
    content_ready_topic: str, stop_event: threading.Event, wake_event: threading.Event,
) -> None:
    """loop_start() + stop_event.wait(), stable client_id +
    clean_session=True, resubscribe on every (re)connect. A message
    published while beacon is briefly offline is lost at the MQTT layer —
    _reconcile_missed_content_ready (see _run_tdma_loop) periodically
    catches up on anything genuinely missed via item_readiness/audit_log."""
    import paho.mqtt.client as mqtt_client

    logger.info("beacon: mqtt starting (content_ready_topic=%s)", content_ready_topic)

    client = mqtt_client.Client(
        mqtt_client.CallbackAPIVersion.VERSION2,
        client_id="radiobeacon-beacon",
        clean_session=True,
    )
    client.on_message = _make_on_message(content_ready_topic, wake_event)

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
    begins."""
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
    service_controller: service_control.ServiceController, svxlink_name: str, direwolf_name: str,
    lead_time: float, prepped_voice: bool, prepped_frame: bool,
) -> tuple[bool, bool]:
    """Stops/starts services ahead of each content slot's start, but only
    when there's actually something scheduled for it. prepped_voice/
    prepped_frame are per-occurrence latches so each occurrence triggers
    exactly once."""
    voice_eta = _seconds_until_slot_start(config, schedule.Slot.VOICE, now)
    frame_eta = _seconds_until_slot_start(config, schedule.Slot.FRAME, now)

    if voice_eta <= lead_time:
        if not prepped_voice and has_pending_tx_schedule(conn, "voice"):
            _switch_service(conn, service_controller, stop_name=direwolf_name, start_name=svxlink_name)
            prepped_voice = True
    else:
        prepped_voice = False

    if frame_eta <= lead_time:
        if not prepped_frame and has_pending_tx_schedule(conn, "frame"):
            _switch_service(conn, service_controller, stop_name=svxlink_name, start_name=direwolf_name)
            prepped_frame = True
    else:
        prepped_frame = False

    return prepped_voice, prepped_frame


def _write_heartbeat(conn, state: schedule.SlotState) -> None:
    set_beacon_status(conn, "process_heartbeat_at", utc_now().isoformat())
    set_beacon_status(conn, "current_slot", state.slot.value)
    set_beacon_status(conn, "current_cycle_index", str(state.cycle_index))
    set_beacon_status(conn, "current_cycle_elapsed_seconds", str(round(state.elapsed_in_cycle, 1)))
    set_beacon_status(conn, "current_slot_remaining_seconds", str(round(state.remaining_in_slot, 1)))
    # Kept under the historical *_queue_depth keys so ui/routers/beacon.py
    # and its templates need no change — these are now pending
    # beacon_tx_schedule row counts per kind. *_dropped_total stays for one
    # release (always 0 now: add_tx_schedule_unit trims silently).
    counts = count_tx_schedule_by_kind(conn)
    set_beacon_status(conn, "voice_queue_depth", str(counts.get("voice", 0)))
    set_beacon_status(conn, "voice_queue_dropped_total", "0")
    set_beacon_status(conn, "frame_queue_depth", str(counts.get("frame", 0)))
    set_beacon_status(conn, "frame_queue_dropped_total", "0")


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
    dt = content.resolve_source_date_time(conn, source, item_id)
    if dt is None:
        return ""
    return to_display_tz(dt, conn=conn).strftime(date_format)


def _transmit_voice_unit(conn, row: dict, ctx: dict) -> bool:
    """Transmits one voice unit. Returns whether the transmission was
    actually sent (a skip — no callsign, no resolvable text — returns
    False but still counts as an attempt at the caller, same as the old
    _try_transmit_voice)."""
    source, item_id = row["source"], row["item_id"]
    callsign = ctx["callsign"]
    if not callsign:
        logger.warning("beacon: BEACON_CALLSIGN not configured, skipping voice transmission")
        record_audit_event(
            conn, event_type="beacon.voice.skipped_no_callsign", actor="beacon",
            source=source, item_id=item_id,
        )
        return False

    text = content.resolve_voice_text(conn, source, item_id)
    if not text:
        logger.info("beacon: source=%s item_id=%s has no resolvable voice text, skipping", source, item_id)
        return False

    date_str = _format_source_date_time(conn, source, item_id, ctx["date_format"])
    item_fields = content.resolve_item_fields(conn, source, item_id)
    item_fields.update(source=source, item_id=item_id)
    formatted = formatters.format_voice(
        text, callsign=callsign, template=ctx["voice_template"], max_chars=ctx["voice_max_chars"],
        prefix=ctx["voice_prefix"], suffix=ctx["voice_suffix"], date=date_str, **item_fields,
    )
    wav_path = Path(ctx["wav_dir"]) / f"{source}-{item_id}-{int(time.time())}.wav"
    if not voice.synthesize_speech(
        formatted.text, out_path=wav_path, voice=ctx["tts_voice"],
        engine=ctx["tts_engine"], piper_model=ctx["tts_piper_model"], piper_binary=ctx["tts_piper_binary"],
    ):
        record_audit_event(
            conn, event_type="beacon.voice.transmit_failed", actor="beacon",
            source=source, item_id=item_id, details={"reason": "tts_failed"},
        )
        return False

    try:
        sent = ctx["voice_transmitter"].transmit(text=formatted.text, wav_path=wav_path)
    except Exception:
        logger.error("beacon: voice transmitter raised", exc_info=True)
        sent = False

    if sent:
        record_audit_event(
            conn, event_type="beacon.voice.transmitted", actor="beacon",
            source=source, item_id=item_id, details={"truncated": formatted.truncated},
        )
        set_beacon_status(conn, "last_voice_transmit_at", utc_now().isoformat())
    else:
        record_audit_event(
            conn, event_type="beacon.voice.transmit_failed", actor="beacon",
            source=source, item_id=item_id,
        )
    return sent


def _transmit_frame_unit(conn, row: dict, ctx: dict) -> bool:
    source, item_id = row["source"], row["item_id"]
    chunk_index = int(row["ref"])
    callsign = ctx["callsign"]
    if not callsign:
        logger.warning("beacon: BEACON_CALLSIGN not configured, skipping frame transmission")
        record_audit_event(
            conn, event_type="beacon.frame.skipped_no_callsign", actor="beacon",
            source=source, item_id=item_id,
        )
        return False

    chunk_text = content.resolve_frame_text(conn, source, item_id, chunk_index)
    if chunk_text is None:
        logger.info(
            "beacon: source=%s item_id=%s chunk_index=%s no longer resolvable, skipping",
            source, item_id, chunk_index,
        )
        return False

    date_str = _format_source_date_time(conn, source, item_id, ctx["date_format"])
    item_fields = content.resolve_item_fields(conn, source, item_id)
    item_fields.update(source=source, item_id=item_id)
    try:
        formatted = formatters.format_frame(
            chunk_text, callsign=callsign, destination=ctx["destination"],
            prefix=ctx["frame_prefix"], suffix=ctx["frame_suffix"], date=date_str, **item_fields,
        )
    except formatters.FrameTooLongError as exc:
        logger.error("beacon: frame too long, dropping: %s", exc)
        record_audit_event(
            conn, event_type="beacon.frame.dropped_too_long", actor="beacon",
            source=source, item_id=item_id, details={"error": str(exc)},
        )
        return False

    sent = ctx["kiss_client"].send_ui_frame(
        source_callsign=callsign, dest_callsign=ctx["destination"], info=formatted.content.encode("utf-8")
    )
    if sent:
        record_audit_event(
            conn, event_type="beacon.frame.transmitted", actor="beacon",
            source=source, item_id=item_id,
            details={"tnc2": formatted.tnc2, "byte_length": formatted.byte_length},
        )
        set_beacon_status(conn, "last_frame_transmit_at", utc_now().isoformat())
    else:
        record_audit_event(
            conn, event_type="beacon.frame.transmit_failed", actor="beacon",
            source=source, item_id=item_id,
        )
    return sent


# kind -> (conn, row, ctx) -> bool. Adding a future repeatable TDMA slot
# type is a new Slot value, a SLOT_KINDS entry, and one entry here.
KIND_TRANSMITTERS = {
    "voice": _transmit_voice_unit,
    "frame": _transmit_frame_unit,
}
SLOT_KINDS = {
    schedule.Slot.VOICE: ("voice",),
    schedule.Slot.FRAME: ("frame",),
}


def _drain_kind(
    stop_event: threading.Event, conn, kind: str, now_dt: datetime, ctx: dict, inter_tx_delay: float,
) -> int:
    """Transmits every currently-due beacon_tx_schedule row of `kind`,
    decrementing the repeat budget and retiring rows at
    sent_count >= repeat_times. Due-ness and retirement are computed live
    against each row's current policy (adapters.transmit_policy.policy_for),
    so editing a tier stays reactive. Every attempt counts. Returns the
    number of rows transmitted (mainly for tests)."""
    transmit = KIND_TRANSMITTERS[kind]
    attempted = 0
    for row in due_tx_schedule_rows(conn, kind):
        if stop_event.is_set():
            break
        policy = policy_for(conn, row["transmit_policy"])

        if row["sent_count"] >= policy.repeat_times:
            record_tx_schedule_sent(
                conn, row["source"], row["item_id"], kind, row["ref"],
                now_iso=now_dt.isoformat(), retire=True,
            )
            _record_retired(conn, row, kind)
            continue

        if row["last_transmitted_at"] is not None:
            due_at = datetime.fromisoformat(row["last_transmitted_at"]) + timedelta(
                seconds=policy.interval_seconds
            )
            if now_dt < due_at:
                continue  # not due yet under the current policy

        if attempted > 0:
            stop_event.wait(inter_tx_delay)
        transmit(conn, row, ctx)
        attempted += 1

        retire = row["sent_count"] + 1 >= policy.repeat_times
        record_tx_schedule_sent(
            conn, row["source"], row["item_id"], kind, row["ref"],
            now_iso=now_dt.isoformat(), retire=retire,
        )
        if retire:
            _record_retired(conn, row, kind)
    return attempted


def _record_retired(conn, row: dict, kind: str) -> None:
    record_audit_event(
        conn, event_type="beacon.tx.retired", actor="beacon",
        source=row["source"], item_id=row["item_id"],
        details={"kind": kind, "ref": row["ref"], "sent_count": row["sent_count"] + 1},
    )


def _run_tdma_loop(stop_event: threading.Event, wake_event: threading.Event) -> None:
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

        prepped_voice = False
        prepped_frame = False
        last_ntp_check_at = 0.0
        last_reconcile_at = 0.0

        logger.info(
            "beacon: TDMA loop starting (kiss=%s:%d, voice_transmitter=%s, service_controller=%s)",
            kiss_host, kiss_port, type(voice_transmitter).__name__, type(service_controller).__name__,
        )

        while not stop_event.is_set():
            now = time.time()
            now_dt = utc_now()
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
            ctx = {
                "callsign": get_setting("BEACON_CALLSIGN", conn=conn, env_fallback=False),
                "voice_template": get_setting("BEACON_VOICE_TEMPLATE", "{callsign}. {text}. {date}", conn=conn),
                "voice_max_chars": int(get_setting("BEACON_VOICE_MAX_CHARS", "500", conn=conn)),
                "date_format": get_setting("BEACON_DATE_FORMAT", "%d-%m-%Y %H:%M", conn=conn),
                "destination": get_setting("BEACON_FRAME_DESTINATION", "NFO", conn=conn),
                "frame_prefix": get_setting("BEACON_FRAME_PREFIX", "", conn=conn) or "",
                "frame_suffix": get_setting("BEACON_FRAME_SUFFIX", "", conn=conn) or "",
                "voice_prefix": get_setting("BEACON_VOICE_PREFIX", "", conn=conn) or "",
                "voice_suffix": get_setting("BEACON_VOICE_SUFFIX", "", conn=conn) or "",
                "wav_dir": get_setting("BEACON_TTS_WAV_DIR", "storage/beacon_tts", conn=conn),
                "tts_voice": get_setting("BEACON_TTS_VOICE", "es", conn=conn),
                "tts_engine": get_setting("BEACON_TTS_ENGINE", "piper", conn=conn),
                "tts_piper_model": get_setting(
                    "BEACON_TTS_PIPER_MODEL", "storage/piper_voices/es_MX-claude-high.onnx", conn=conn
                ),
                "tts_piper_binary": get_setting("BEACON_TTS_PIPER_BINARY", "piper", conn=conn),
                "voice_transmitter": voice_transmitter,
                "kiss_client": kiss_client,
            }
            voice_inter_tx_delay = float(get_setting("BEACON_VOICE_INTER_TX_DELAY_SECONDS", "2", conn=conn))
            frame_inter_tx_delay = float(get_setting("BEACON_FRAME_INTER_TX_DELAY_SECONDS", "2", conn=conn))
            inter_tx_delay = {"voice": voice_inter_tx_delay, "frame": frame_inter_tx_delay}

            if now - last_ntp_check_at >= ntp_interval:
                _run_ntp_check(conn)
                last_ntp_check_at = now

            if now - last_reconcile_at >= reconcile_interval:
                reconciled = _reconcile_missed_content_ready(conn)
                if reconciled:
                    logger.warning("beacon: reconciled %d missed item.content_ready publish(es)", reconciled)
                last_reconcile_at = now

            state = schedule.current_slot(window, now)
            _write_heartbeat(conn, state)

            prepped_voice, prepped_frame = _maybe_control_services(
                conn, window, now, service_controller,
                svxlink_name, direwolf_name, lead_time, prepped_voice, prepped_frame,
            )

            if enabled:
                for kind in SLOT_KINDS.get(state.slot, ()):
                    _drain_kind(stop_event, conn, kind, now_dt, ctx, inter_tx_delay[kind])

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
    conn.close()

    stop_event = threading.Event()
    wake_event = threading.Event()

    def _handle_shutdown_signal(signum, frame) -> None:
        logger.info("received signal %d, shutting down", signum)
        stop_event.set()
        wake_event.set()

    signal.signal(signal.SIGINT, _handle_shutdown_signal)
    signal.signal(signal.SIGTERM, _handle_shutdown_signal)

    mqtt_thread = threading.Thread(
        target=_run_mqtt_client,
        args=(content_ready_topic, stop_event, wake_event),
        name="beacon-mqtt",
        daemon=True,
    )
    tdma_thread = threading.Thread(
        target=_run_tdma_loop,
        args=(stop_event, wake_event),
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
