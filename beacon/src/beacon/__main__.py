"""The transmit loop — beacon's entry point.

Content flows in from a single MQTT subscription: item.content_ready
(actions.content_ready), published only once BOTH actions.chunk and
actions.ai have finished reacting to the same dispatch — a race-free
"everything that was going to happen to this item's content has happened"
signal. On each item.content_ready, beacon schedules transmit rows for the
ONE configured beacon type (BEACON_TYPE): either a single kind="voice" row,
or one kind="frame" row per existing `chunks` row. Rows go into the durable
`beacon_tx_schedule` table (adapters.storage), snapshotting the item's
Policy NAME. That table — not an in-memory queue — is the source of truth
for what still has to go on air and how many more times, so a beacon
restart no longer loses pending transmissions.

How often each row is transmitted, and how far apart, is resolved live
every cycle from the Policy's transmit stage via adapters.policy.policy_for
(kind once/interval/cron, count, interval_seconds, cron) — so editing a
Policy (dispatcher/policies.sh or the ui's /config/policies) changes
in-flight behavior on the next cycle. A row is deleted once sent_count
reaches transmit_count. Every transmission *attempt* counts against the
budget (a run of failures still retires the row).

Both beacon types end the same way: a WAV file is rendered (piper/espeak
for voice, Direwolf's `gen_packets` CLI for frame) and handed to
transmit.WavTransmitter — which, in production, drops it into the
svxlink-txqueue spool folder for SvxLink to play when the RF channel is
idle (see documentation/svxlink-txqueue-SETUP.md). There is no TDMA
schedule, no Direwolf service, and no sound-card contention to arbitrate:
SvxLink is the only thing that touches the radio.

See content.py for how the actual text gets resolved (lazily, at transmit
time, not baked in at schedule time) and formatters.py for how each
type's length limit is sourced: frame reuses actions.chunk's
ACTIONS_CHUNK_MAX_CHARS, while voice has its own dedicated
BEACON_VOICE_MAX_CHARS. Each type also has its own prefix/suffix
(BEACON_FRAME_PREFIX/SUFFIX, BEACON_VOICE_PREFIX/SUFFIX) wrapping its
content, plus voice's outer BEACON_VOICE_TEMPLATE — all three str.format
templates, sharing one placeholder vocabulary beyond {date}: {source},
{item_id}, {type}, {subtype}, {extracted_title}, {url}, {source_name},
{source_url} (see content.resolve_item_fields, resolved fresh per item at
transmit time, same as the text itself).

BEACON_ENABLED (decision: soft enable/disable, not real process control —
see beacon/README.md) is re-read every tick; scheduling rows from MQTT
happens unconditionally regardless of it — only the transmit step checks
it.

BEACON_WATERMARK_ENABLED adds a second, independent transmit path: a
periodic message on its own BEACON_WATERMARK_INTERVAL_SECONDS timer,
checked once per tick alongside the NTP/reconcile checks below, bypassing
beacon_tx_schedule entirely (see _transmit_watermark). It has no item
behind it, so its BEACON_WATERMARK_VOICE_TEMPLATE/BEACON_WATERMARK_FRAME_TEMPLATE
placeholders are the beacon's own per-tick ctx attributes (callsign, date,
destination, ...) rather than item fields — see _watermark_fields. Also
gated by BEACON_ENABLED, same as regular content.

The loop's wait between ticks is a wake_event.wait(timeout=tick_seconds),
not a plain sleep -- _handle_content_ready_event sets wake_event right
after scheduling rows (both the live MQTT path and
_reconcile_missed_content_ready's catch-up path), so newly-scheduled
content is picked up on the very next loop iteration."""
import logging
import signal
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

from adapters.attention_tone import prepend_tone_to_wav
from adapters.beacon_defaults import (
    BEACON_ENABLED_DEFAULT,
    BEACON_MANUAL_VOICE_TEMPLATE_DEFAULT,
    BEACON_MAX_QUEUED_AGE_SECONDS_DEFAULT,
    BEACON_QUEUE_MAX_SIZE_DEFAULT,
    BEACON_TTS_RETENTION_DAYS_DEFAULT,
    BEACON_TYPE_DEFAULT,
    BEACON_VOICE_ATTENTION_TONE_DEFAULT,
    BEACON_VOICE_MAX_CHARS_DEFAULT,
    BEACON_VOICE_PREFIX_DEFAULT,
    BEACON_VOICE_SUFFIX_DEFAULT,
    BEACON_VOICE_TEMPLATE_DEFAULT,
    BEACON_WATERMARK_ENABLED_DEFAULT,
    BEACON_WATERMARK_FRAME_TEMPLATE_DEFAULT,
    BEACON_WATERMARK_INTERVAL_SECONDS_DEFAULT,
    BEACON_WATERMARK_VOICE_TEMPLATE_DEFAULT,
)
from adapters.storage import (
    DEFAULT_DB_PATH,
    add_tx_schedule_unit,
    count_tx_schedule_by_kind,
    delete_manual_tx,
    delete_stale_tx_schedule,
    delete_tx_schedule_other_kinds,
    due_tx_schedule_rows,
    get_connection,
    get_setting,
    pending_manual_tx,
    record_audit_event,
    record_tx_schedule_sent,
    set_beacon_status,
)
from adapters.cron import latest_fire_at_or_before
from adapters.logsetup import configure_logging, refresh_level
from adapters.timeutil import to_display_tz, utc_now
from adapters.policy import policy_for
from adapters.voice_replacements import apply_voice_replacements

from beacon import content, formatters, frame_audio, mq, ntp, transmit, tx_monitor, voice

# Repo root — for anchoring a relative BEACON_TTS_WAV_DIR (see
# _resolve_wav_dir). beacon/ runs from source (PYTHONPATH=src, `python -m
# beacon`), so beacon/src/beacon/__main__.py -> parents[3] is the repo root.
REPO_ROOT = Path(__file__).resolve().parents[3]

logger = logging.getLogger("beacon")

BEACON_MQ_HOST = get_setting("BEACON_MQ_HOST", "localhost")
BEACON_MQ_PORT = int(get_setting("BEACON_MQ_PORT", "1883"))
BEACON_MQ_QOS = int(get_setting("BEACON_MQ_QOS", "1"))
BEACON_MQ_RECONNECT_BACKOFF_SECONDS = int(get_setting("BEACON_MQ_RECONNECT_BACKOFF_SECONDS", "5"))

VALID_BEACON_TYPES = ("voice", "frame")


def _resolve_wav_dir(raw: str) -> str:
    """Anchor a relative BEACON_TTS_WAV_DIR at the repo root rather than the
    beacon process's cwd (which is beacon/). The default therefore lands in
    the top-level storage/ directory the ui/ container also bind-mounts, so
    the dashboard can serve the rendered clips back (see
    ui.beacon_audio). An absolute path is used verbatim."""
    path = Path(raw).expanduser()
    return str(path if path.is_absolute() else REPO_ROOT / path)


def _resolve_beacon_type(conn) -> str:
    value = (get_setting("BEACON_TYPE", BEACON_TYPE_DEFAULT, conn=conn) or "").strip().lower()
    if value not in VALID_BEACON_TYPES:
        logger.error("invalid BEACON_TYPE=%r, falling back to %r", value, BEACON_TYPE_DEFAULT)
        return BEACON_TYPE_DEFAULT
    return value


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


def _superseded_by_newer(conn, source: str, item_id: str, event_key: str, kind: str) -> bool:
    """True if a *newer* item for the same (source, event_key) already has a
    live beacon_tx_schedule row of this kind — so this (older) item must not
    be scheduled. Keyed on event_key, which means "the same ongoing thing";
    a null event_key never reaches here."""
    row = conn.execute(
        "SELECT 1 FROM beacon_tx_schedule s JOIN items i "
        "ON i.source = s.source AND i.item_id = s.item_id "
        "WHERE s.source = ? AND s.kind = ? AND i.event_key = ? AND s.item_id != ? "
        "AND i.source_date_time > (SELECT source_date_time FROM items WHERE source=? AND item_id=?) "
        "LIMIT 1",
        (source, kind, event_key, item_id, source, item_id),
    ).fetchone()
    return row is not None


def _retire_older_for_event_key(conn, source: str, item_id: str, event_key: str, kind: str) -> None:
    """Delete pending beacon_tx_schedule rows for *older* items sharing this
    (source, event_key) — the fresher item takes over. Already-sent airings
    stand; only pending rows go."""
    older = conn.execute(
        "SELECT s.item_id, s.ref FROM beacon_tx_schedule s JOIN items i "
        "ON i.source = s.source AND i.item_id = s.item_id "
        "WHERE s.source = ? AND s.kind = ? AND i.event_key = ? AND s.item_id != ? "
        "AND i.source_date_time < (SELECT source_date_time FROM items WHERE source=? AND item_id=?)",
        (source, kind, event_key, item_id, source, item_id),
    ).fetchall()
    for old_item_id, ref in older:
        conn.execute(
            "DELETE FROM beacon_tx_schedule "
            "WHERE source = ? AND item_id = ? AND kind = ? AND ref = ?",
            (source, old_item_id, kind, ref),
        )
        record_audit_event(
            conn, event_type="beacon.tx.superseded", actor="beacon",
            source=source, item_id=old_item_id,
            details={"kind": kind, "ref": ref, "superseded_by": item_id, "event_key": event_key},
        )
    if older:
        conn.commit()


def _handle_content_ready_event(
    conn, source: str, item_id: str, event_id: str | None,
    wake_event: threading.Event | None = None,
) -> None:
    """Schedules beacon_tx_schedule rows for an item, for the ONE configured
    BEACON_TYPE: a single kind="voice" row, or one kind="frame" row per existing
    `chunks` row. A fresher item for the same event_key supersedes older
    pending rows; an already-superseded item is skipped. wake_event
    (optional -- None from _reconcile_missed_content_ready) is set after
    scheduling so the transmit loop's tick-wait returns immediately."""
    if _already_enqueued(conn, event_id):
        logger.info("content_ready event_id=%s already scheduled, skipping", event_id)
        return

    beacon_type = _resolve_beacon_type(conn)

    item_row = conn.execute(
        "SELECT policy, event_key, source_date_time FROM items WHERE source = ? AND item_id = ?",
        (source, item_id),
    ).fetchone()
    policy = item_row[0] if item_row is not None else None
    event_key = item_row[1] if item_row is not None else None

    if event_key and _superseded_by_newer(conn, source, item_id, event_key, beacon_type):
        record_audit_event(
            conn, event_type="beacon.tx.supersede_skip", actor="beacon",
            source=source, item_id=item_id, details={"event_key": event_key},
        )
        return
    if event_key:
        _retire_older_for_event_key(conn, source, item_id, event_key, beacon_type)

    max_size = int(get_setting("BEACON_QUEUE_MAX_SIZE", BEACON_QUEUE_MAX_SIZE_DEFAULT, conn=conn))

    frame_count = 0
    if beacon_type == "frame":
        rows = conn.execute(
            "SELECT chunk_index FROM chunks WHERE source = ? AND item_id = ? ORDER BY chunk_index",
            (source, item_id),
        ).fetchall()
        for (chunk_index,) in rows:
            add_tx_schedule_unit(
                conn, source, item_id, "frame", str(chunk_index),
                policy, event_id, max_size=max_size,
            )
        frame_count = len(rows)
    else:
        add_tx_schedule_unit(
            conn, source, item_id, "voice", "", policy, event_id, max_size=max_size
        )

    if wake_event is not None:
        wake_event.set()

    record_audit_event(
        conn, event_type="beacon.content_ready.enqueued", actor="beacon", source=source, item_id=item_id,
        details={"event_id": event_id, "beacon_type": beacon_type, "frame_count": frame_count},
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
            "reconciling missed item.content_ready for source=%s item_id=%s "
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
                    "event missing source/item_id, skipping topic=%s data=%r", message.topic, data
                )
                return

            conn = get_connection(DEFAULT_DB_PATH)
            try:
                if message.topic == content_ready_topic:
                    _handle_content_ready_event(conn, source, item_id, event_id, wake_event)
                else:
                    logger.warning("message on unexpected topic %s", message.topic)
            finally:
                conn.close()
        except Exception:
            logger.error("failed handling message on %s", message.topic, exc_info=True)

    return _handler


def _run_mqtt_client(
    content_ready_topic: str, stop_event: threading.Event, wake_event: threading.Event,
) -> None:
    """loop_start() + stop_event.wait(), stable client_id +
    clean_session=True, resubscribe on every (re)connect. A message
    published while beacon is briefly offline is lost at the MQTT layer —
    _reconcile_missed_content_ready (see _run_transmit_loop) periodically
    catches up on anything genuinely missed via item_readiness/audit_log."""
    import paho.mqtt.client as mqtt_client

    logger.info("mqtt starting topic=%s", content_ready_topic)

    client = mqtt_client.Client(
        mqtt_client.CallbackAPIVersion.VERSION2,
        client_id="radiobeacon-beacon",
        clean_session=True,
    )
    client.on_message = _make_on_message(content_ready_topic, wake_event)

    def _on_connect(client, userdata, connect_flags, reason_code, properties) -> None:
        logger.debug("mqtt connected, subscribing reason_code=%s", reason_code)
        client.subscribe([(content_ready_topic, BEACON_MQ_QOS)])

    client.on_connect = _on_connect

    while not stop_event.is_set():
        try:
            client.connect(BEACON_MQ_HOST, BEACON_MQ_PORT)
            break
        except Exception:
            logger.error(
                "mqtt failed to connect to %s:%d, retrying in %ds",
                BEACON_MQ_HOST, BEACON_MQ_PORT, BEACON_MQ_RECONNECT_BACKOFF_SECONDS,
                exc_info=True,
            )
            stop_event.wait(BEACON_MQ_RECONNECT_BACKOFF_SECONDS)
    else:
        logger.info("mqtt stopped before connecting")
        return

    client.loop_start()
    stop_event.wait()
    client.loop_stop()
    client.disconnect()
    logger.info("mqtt stopped")


# --- transmit loop ---


def _write_heartbeat(conn, beacon_type: str) -> None:
    set_beacon_status(conn, "process_heartbeat_at", utc_now().isoformat())
    set_beacon_status(conn, "beacon_type", beacon_type)
    # Kept under the historical *_queue_depth keys so ui/routers/beacon.py
    # and its templates need no change — these are pending beacon_tx_schedule
    # row counts per kind (only the active type is ever non-zero now).
    counts = count_tx_schedule_by_kind(conn)
    set_beacon_status(conn, "voice_queue_depth", str(counts.get("voice", 0)))
    set_beacon_status(conn, "frame_queue_depth", str(counts.get("frame", 0)))


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
                "NTP offset %.3fs exceeds max allowed %.3fs server=%s",
                result.offset_seconds, max_offset, server,
            )
    record_audit_event(
        conn, event_type="beacon.ntp.checked", actor="beacon",
        details={
            "ok": result.ok, "offset_seconds": result.offset_seconds,
            "method": result.method, "error": result.error,
        },
    )


def _build_wav_transmitter(conn) -> transmit.WavTransmitter:
    kind = get_setting("BEACON_WAV_TRANSMITTER", "logging", conn=conn)
    if kind == "spool":
        incoming_dir = get_setting(
            "BEACON_TXQUEUE_INCOMING_DIR", "/var/spool/svxlink-tx/incoming", conn=conn
        )
        return transmit.SpoolWavTransmitter(incoming_dir)
    return transmit.LoggingWavTransmitter()


def _format_source_date_time(conn, source: str, item_id: str, date_format: str) -> str:
    dt = content.resolve_source_date_time(conn, source, item_id)
    if dt is None:
        return ""
    return to_display_tz(dt, conn=conn).strftime(date_format)


def _transmit_voice_unit(conn, row: dict, ctx: dict) -> bool:
    """Renders one voice unit to a WAV and hands it to the WAV transmitter.
    Returns whether it was actually sent (a skip — no callsign, no resolvable
    text — returns False but still counts as an attempt at the caller)."""
    source, item_id = row["source"], row["item_id"]
    callsign = ctx["callsign"]
    if not callsign:
        logger.warning("BEACON_CALLSIGN not configured, skipping voice transmission")
        record_audit_event(
            conn, event_type="beacon.voice.skipped_no_callsign", actor="beacon",
            source=source, item_id=item_id,
        )
        return False

    text = content.resolve_voice_text(conn, source, item_id)
    if not text:
        logger.info("source=%s item_id=%s has no resolvable voice text, skipping", source, item_id)
        return False

    # Expand this adapter's configured abbreviations for TTS, before
    # format_voice truncates/wraps — so the spoken-length budget counts the
    # expanded words and a truncation never splits one. Voice channel only;
    # the stored summary and any AX.25 frame text keep the abbreviations.
    # The watermark and manual "Transmit now" voice paths have no source
    # adapter, so this deliberately doesn't reach them.
    text = apply_voice_replacements(text, content.resolve_voice_replacements(conn, source))

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

    if ctx["voice_attention_tone"]:
        prepend_tone_to_wav(wav_path, ctx["voice_attention_tone"])

    transmit_error: str | None = None
    try:
        sent = ctx["wav_transmitter"].transmit(
            wav_path=wav_path, label=f"voice {source}/{item_id}"
        )
    except Exception as exc:
        logger.error("wav transmitter raised", exc_info=True)
        sent = False
        transmit_error = str(exc)
    if not sent and transmit_error is None:
        # SpoolWavTransmitter catches its own OSError (e.g. a permission
        # error on the spool dir) and returns False rather than raising —
        # last_error carries that cause through to the audit row instead of
        # only the generic "transmitter_failed" marker below.
        transmit_error = getattr(ctx["wav_transmitter"], "last_error", None)

    if sent:
        record_audit_event(
            conn, event_type="beacon.voice.transmitted", actor="beacon",
            source=source, item_id=item_id,
            details={"truncated": formatted.truncated, "clip": wav_path.name},
        )
        set_beacon_status(conn, "last_voice_transmit_at", utc_now().isoformat())
    else:
        record_audit_event(
            conn, event_type="beacon.voice.transmit_failed", actor="beacon",
            source=source, item_id=item_id,
            details={"reason": transmit_error or "transmitter_failed"},
        )
    return sent


def _transmit_frame_unit(conn, row: dict, ctx: dict) -> bool:
    source, item_id = row["source"], row["item_id"]
    chunk_index = int(row["ref"])
    callsign = ctx["callsign"]
    if not callsign:
        logger.warning("BEACON_CALLSIGN not configured, skipping frame transmission")
        record_audit_event(
            conn, event_type="beacon.frame.skipped_no_callsign", actor="beacon",
            source=source, item_id=item_id,
        )
        return False

    chunk_text = content.resolve_frame_text(conn, source, item_id, chunk_index)
    if chunk_text is None:
        logger.info(
            "source=%s item_id=%s chunk_index=%s no longer resolvable, skipping",
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
        logger.error("frame too long, dropping reason=%s", exc)
        record_audit_event(
            conn, event_type="beacon.frame.dropped_too_long", actor="beacon",
            source=source, item_id=item_id, details={"error": str(exc)},
        )
        return False

    wav_path = Path(ctx["wav_dir"]) / f"{source}-{item_id}-{chunk_index}-{int(time.time())}.wav"
    if not frame_audio.synthesize_frame_wav(
        formatted.tnc2, out_path=wav_path,
        gen_packets_binary=ctx["gen_packets_binary"], lead_silence_ms=ctx["frame_lead_silence_ms"],
    ):
        record_audit_event(
            conn, event_type="beacon.frame.transmit_failed", actor="beacon",
            source=source, item_id=item_id, details={"reason": "gen_packets_failed"},
        )
        return False

    transmit_error: str | None = None
    try:
        sent = ctx["wav_transmitter"].transmit(
            wav_path=wav_path, label=f"frame {source}/{item_id} {chunk_index}"
        )
    except Exception as exc:
        logger.error("wav transmitter raised", exc_info=True)
        sent = False
        transmit_error = str(exc)
    if not sent and transmit_error is None:
        transmit_error = getattr(ctx["wav_transmitter"], "last_error", None)

    if sent:
        record_audit_event(
            conn, event_type="beacon.frame.transmitted", actor="beacon",
            source=source, item_id=item_id,
            details={
                "tnc2": formatted.tnc2, "byte_length": formatted.byte_length,
                "ref": chunk_index, "clip": wav_path.name,
            },
        )
        set_beacon_status(conn, "last_frame_transmit_at", utc_now().isoformat())
    else:
        record_audit_event(
            conn, event_type="beacon.frame.transmit_failed", actor="beacon",
            source=source, item_id=item_id,
            details={"reason": transmit_error or "transmitter_failed"},
        )
    return sent


# callsign is always passed to format_voice/format_frame as its own named
# parameter below -- excluded here so it doesn't collide as a duplicate
# keyword argument; still available to templates via that named parameter.
# voice_attention_tone is an audio concern with no meaning as a spoken
# template placeholder, and the watermark deliberately carries no tone.
_WATERMARK_FIELDS_EXCLUDE = {"callsign", "voice_attention_tone"}


def _watermark_fields(ctx: dict) -> dict[str, str]:
    """Beacon's own per-tick ctx, flattened to str values -- the "all
    beacon attributes" available as BEACON_WATERMARK_VOICE_TEMPLATE/
    BEACON_WATERMARK_FRAME_TEMPLATE placeholders. A watermark has no item
    behind it, so unlike voice/frame content it can't offer
    content.resolve_item_fields()'s {type}/{subtype}/{url}/... -- this is
    the beacon-level equivalent. wav_transmitter (not a str/int/float) is
    excluded by the isinstance filter, same as any other non-placeholder
    value a future ctx entry might add."""
    return {
        k: str(v) for k, v in ctx.items()
        if isinstance(v, (str, int, float)) and k not in _WATERMARK_FIELDS_EXCLUDE
    }


def _transmit_watermark(conn, beacon_type: str, ctx: dict, now_dt: datetime) -> bool:
    """Renders and transmits the periodic watermark message for whichever
    beacon type is currently active. Independent of beacon_tx_schedule --
    no row, no Policy transmit budget, just the caller's
    own BEACON_WATERMARK_INTERVAL_SECONDS timer. Returns whether it was
    actually sent."""
    callsign = ctx["callsign"]
    if not callsign:
        logger.warning("BEACON_CALLSIGN not configured, skipping watermark transmission")
        record_audit_event(conn, event_type="beacon.watermark.skipped_no_callsign", actor="beacon")
        return False

    date_str = to_display_tz(now_dt, conn=conn).strftime(ctx["date_format"])
    watermark_fields = _watermark_fields(ctx)
    wav_path = Path(ctx["wav_dir"]) / f"watermark-{int(time.time())}.wav"

    if beacon_type == "voice":
        formatted = formatters.format_voice(
            "", callsign=callsign, template=ctx["watermark_voice_template"],
            max_chars=0,  # text is always "" here -- the template is the whole message
            prefix="", suffix="", date=date_str, **watermark_fields,
        )
        if not formatted.text.strip():
            logger.info("watermark voice template rendered empty, skipping")
            return False
        if not voice.synthesize_speech(
            formatted.text, out_path=wav_path, voice=ctx["tts_voice"],
            engine=ctx["tts_engine"], piper_model=ctx["tts_piper_model"], piper_binary=ctx["tts_piper_binary"],
        ):
            record_audit_event(
                conn, event_type="beacon.watermark.transmit_failed", actor="beacon",
                details={"reason": "tts_failed"},
            )
            return False
    elif beacon_type == "frame":
        # destination is also an explicit named parameter here (unlike
        # format_voice, which has no destination at all) -- drop it from
        # the overflow kwargs to avoid the same duplicate-keyword collision.
        frame_watermark_fields = {k: v for k, v in watermark_fields.items() if k != "destination"}
        try:
            formatted_frame = formatters.format_frame(
                "", callsign=callsign, destination=ctx["destination"],
                prefix=ctx["watermark_frame_template"], suffix="", date=date_str, **frame_watermark_fields,
            )
        except formatters.FrameTooLongError as exc:
            logger.error("watermark frame too long, dropping reason=%s", exc)
            record_audit_event(
                conn, event_type="beacon.watermark.dropped_too_long", actor="beacon",
                details={"error": str(exc)},
            )
            return False
        if not formatted_frame.content.strip():
            logger.info("watermark frame template rendered empty, skipping")
            return False
        if not frame_audio.synthesize_frame_wav(
            formatted_frame.tnc2, out_path=wav_path,
            gen_packets_binary=ctx["gen_packets_binary"], lead_silence_ms=ctx["frame_lead_silence_ms"],
        ):
            record_audit_event(
                conn, event_type="beacon.watermark.transmit_failed", actor="beacon",
                details={"reason": "gen_packets_failed"},
            )
            return False
    else:
        return False

    transmit_error: str | None = None
    try:
        sent = ctx["wav_transmitter"].transmit(wav_path=wav_path, label="watermark")
    except Exception as exc:
        logger.error("wav transmitter raised", exc_info=True)
        sent = False
        transmit_error = str(exc)
    finally:
        # A watermark has no item and is never replayed from the UI (unlike
        # voice/frame/manual clips), so unlike those it doesn't need to wait
        # for the hourly retention sweep -- remove it as soon as the
        # transmitter has had its chance to read it.
        wav_path.unlink(missing_ok=True)
    if not sent and transmit_error is None:
        transmit_error = getattr(ctx["wav_transmitter"], "last_error", None)

    if sent:
        record_audit_event(
            conn, event_type="beacon.watermark.transmitted", actor="beacon",
            details={"clip": wav_path.name, "kind": beacon_type},
        )
        set_beacon_status(conn, "last_watermark_transmit_at", utc_now().isoformat())
    else:
        record_audit_event(
            conn, event_type="beacon.watermark.transmit_failed", actor="beacon",
            details={"kind": beacon_type, "reason": transmit_error or "transmitter_failed"},
        )
    return sent


def _transmit_manual_unit(conn, row: dict, beacon_type: str, ctx: dict, now_dt: datetime) -> bool:
    """Renders and transmits one operator-typed manual message
    (beacon_manual_tx). No item, no Policy — sent once. The row is
    deleted by the caller regardless of the outcome."""
    callsign = ctx["callsign"]
    text = (row["text"] or "").strip()
    if not callsign:
        logger.warning("BEACON_CALLSIGN not configured, dropping manual transmission")
        record_audit_event(
            conn, event_type="beacon.manual.skipped_no_callsign", actor="beacon",
            details={"kind": beacon_type},
        )
        return False
    if not text:
        return False

    date_str = to_display_tz(now_dt, conn=conn).strftime(ctx["date_format"])
    wav_path = Path(ctx["wav_dir"]) / f"manual-{row['id']}-{int(time.time())}.wav"

    if beacon_type == "voice":
        formatted = formatters.format_voice(
            text, callsign=callsign, template=ctx["manual_voice_template"],
            max_chars=ctx["voice_max_chars"], prefix="", suffix="", date=date_str,
        )
        if not formatted.text.strip():
            logger.info("manual voice message rendered empty, dropping")
            return False
        if not voice.synthesize_speech(
            formatted.text, out_path=wav_path, voice=ctx["tts_voice"],
            engine=ctx["tts_engine"], piper_model=ctx["tts_piper_model"], piper_binary=ctx["tts_piper_binary"],
        ):
            record_audit_event(
                conn, event_type="beacon.manual.transmit_failed", actor="beacon",
                details={"kind": "voice", "reason": "tts_failed"},
            )
            return False
        if ctx["voice_attention_tone"]:
            prepend_tone_to_wav(wav_path, ctx["voice_attention_tone"])
        label = "manual voice"
    elif beacon_type == "frame":
        try:
            formatted = formatters.format_frame(
                text, callsign=callsign, destination=ctx["destination"],
                prefix="", suffix="", date=date_str,
            )
        except formatters.FrameTooLongError as exc:
            logger.error("manual frame too long, dropping reason=%s", exc)
            record_audit_event(
                conn, event_type="beacon.manual.dropped_too_long", actor="beacon",
                details={"kind": "frame", "error": str(exc)},
            )
            return False
        if not frame_audio.synthesize_frame_wav(
            formatted.tnc2, out_path=wav_path,
            gen_packets_binary=ctx["gen_packets_binary"], lead_silence_ms=ctx["frame_lead_silence_ms"],
        ):
            record_audit_event(
                conn, event_type="beacon.manual.transmit_failed", actor="beacon",
                details={"kind": "frame", "reason": "gen_packets_failed"},
            )
            return False
        label = "manual frame"
    else:
        return False

    transmit_error: str | None = None
    try:
        sent = ctx["wav_transmitter"].transmit(wav_path=wav_path, label=label)
    except Exception as exc:
        logger.error("wav transmitter raised", exc_info=True)
        sent = False
        transmit_error = str(exc)
    if not sent and transmit_error is None:
        transmit_error = getattr(ctx["wav_transmitter"], "last_error", None)

    if sent:
        record_audit_event(
            conn, event_type="beacon.manual.transmitted", actor="beacon",
            details={
                "kind": beacon_type, "chars": len(text), "manual_id": row["id"],
                "clip": wav_path.name,
            },
        )
        set_beacon_status(conn, "last_manual_transmit_at", utc_now().isoformat())
    else:
        record_audit_event(
            conn, event_type="beacon.manual.transmit_failed", actor="beacon",
            details={
                "kind": beacon_type, "manual_id": row["id"],
                "reason": transmit_error or "transmitter_failed",
            },
        )
    return sent


def _drain_manual_tx(
    stop_event: threading.Event, conn, beacon_type: str, now_dt: datetime, ctx: dict,
    inter_tx_delay: float,
) -> int:
    """Transmits every queued manual message for the active beacon_type,
    oldest first, deleting each row once it's been attempted (a manual send
    is one-shot — never retried, whether it went on air or not). Messages
    composed for the other kind wait untouched until the operator switches
    BEACON_TYPE. Returns the number attempted (mainly for tests)."""
    attempted = 0
    for row in pending_manual_tx(conn, beacon_type):
        if stop_event.is_set():
            break
        if attempted > 0:
            stop_event.wait(inter_tx_delay)
        _transmit_manual_unit(conn, row, beacon_type, ctx, now_dt)
        delete_manual_tx(conn, row["id"])
        attempted += 1
    return attempted


# kind -> (conn, row, ctx) -> bool
KIND_TRANSMITTERS = {
    "voice": _transmit_voice_unit,
    "frame": _transmit_frame_unit,
}


def _transmit_due(conn, sched, row: dict, now_dt: datetime) -> bool:
    """Whether this schedule row is due to air now under its transmit
    Schedule. `once` -> only if never sent. `interval` -> last + spacing.
    `cron` -> a cron occurrence strictly after `last_transmitted_at or
    created_at` (so an item armed mid-day first airs at the next slot, not
    immediately, deliberately unlike fetch-cron which catches up on start)."""
    last = row["last_transmitted_at"]
    if sched.kind == "cron":
        since = (last or row["created_at"] or "").replace(" ", "T")
        occ = latest_fire_at_or_before(sched.cron or "", now_dt, conn=conn)
        if occ is None:
            return False
        return occ.isoformat() > since
    if sched.kind == "interval":
        if last is None:
            return True
        return now_dt >= datetime.fromisoformat(last) + timedelta(seconds=sched.interval_seconds)
    # once
    return last is None


def _drain_kind(
    stop_event: threading.Event, conn, kind: str, now_dt: datetime, ctx: dict, inter_tx_delay: float,
) -> int:
    """Transmits every currently-due beacon_tx_schedule row of `kind`,
    decrementing the repeat budget and retiring rows at
    sent_count >= transmit_count. Due-ness and retirement are computed live
    against each row's current Policy transmit stage
    (adapters.policy.policy_for) — kind once/interval/cron — so editing a
    Policy stays reactive. Every attempt counts. Returns the number of
    rows transmitted (mainly for tests)."""
    transmit_fn = KIND_TRANSMITTERS[kind]
    attempted = 0
    for row in due_tx_schedule_rows(conn, kind):
        if stop_event.is_set():
            break
        sched = policy_for(conn, row["policy"])
        cap = sched.count if sched.count is not None else 1

        if row["sent_count"] >= cap:
            record_tx_schedule_sent(
                conn, row["source"], row["item_id"], kind, row["ref"],
                now_iso=now_dt.isoformat(), retire=True,
            )
            _record_retired(conn, row, kind)
            continue

        if not _transmit_due(conn, sched, row, now_dt):
            continue

        if attempted > 0:
            stop_event.wait(inter_tx_delay)
        transmit_fn(conn, row, ctx)
        attempted += 1

        retire = row["sent_count"] + 1 >= cap
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


def _purge_stale_rows(conn, kind: str, max_age_seconds: int, now_dt: datetime) -> int:
    """Drops beacon_tx_schedule rows of `kind` that have waited longer than
    BEACON_MAX_QUEUED_AGE_SECONDS without going on air (measured against
    `updated_at`), recording a beacon.tx.skipped_stale audit event per row.
    Runs every tick regardless of BEACON_ENABLED so a long disabled window
    doesn't leave a stale backlog to dump on air when it's re-enabled.
    Returns the number dropped. A no-op when max_age_seconds <= 0."""
    stale = delete_stale_tx_schedule(
        conn, kind, max_age_seconds=max_age_seconds, now_iso=now_dt.isoformat()
    )
    for row in stale:
        try:
            age_seconds = int(
                (now_dt - datetime.fromisoformat(row["updated_at"]).replace(
                    tzinfo=timezone.utc
                )).total_seconds()
            )
        except (ValueError, TypeError):
            age_seconds = None
        record_audit_event(
            conn, event_type="beacon.tx.skipped_stale", actor="beacon",
            source=row["source"], item_id=row["item_id"],
            details={
                "kind": kind, "ref": row["ref"], "updated_at": row["updated_at"],
                "age_seconds": age_seconds, "max_age_seconds": max_age_seconds,
            },
        )
    if stale:
        logger.info(
            "dropped %d stale %s transmit row(s) (older than %ds, unsent)",
            len(stale), kind, max_age_seconds,
        )
    return len(stale)


def _prune_old_wavs(wav_dir, retention_days: int, now: float) -> int:
    """Deletes rendered WAV clips older than `retention_days` from `wav_dir`.
    No-op when retention_days <= 0. Every transmission (and each frame chunk,
    and each repeat under an interval/cron Policy) writes a fresh timestamped
    file that is never otherwise removed — this keeps the directory bounded.
    Best-effort: an unlink that fails is logged and skipped, never blocks the
    loop."""
    if retention_days <= 0:
        return 0
    cutoff = now - retention_days * 86400
    removed = 0
    try:
        entries = list(Path(wav_dir).iterdir())
    except OSError:
        return 0
    for entry in entries:
        try:
            if entry.is_file() and entry.name.endswith(".wav") and entry.stat().st_mtime < cutoff:
                entry.unlink()
                removed += 1
        except OSError:
            logger.warning("could not prune %s", entry, exc_info=True)
    if removed:
        logger.info("pruned %d WAV clip(s) older than %d day(s)", removed, retention_days)
    return removed


def _run_transmit_loop(stop_event: threading.Event, wake_event: threading.Event) -> None:
    conn = get_connection(DEFAULT_DB_PATH)
    try:
        set_beacon_status(conn, "process_started_at", utc_now().isoformat())

        last_beacon_type = _resolve_beacon_type(conn)
        removed = delete_tx_schedule_other_kinds(conn, last_beacon_type)
        if removed:
            logger.info(
                "cleared %d stale transmit row(s) not matching BEACON_TYPE=%s",
                removed, last_beacon_type,
            )

        last_ntp_check_at = 0.0
        last_reconcile_at = 0.0
        last_watermark_at = 0.0
        last_prune_at = 0.0

        logger.info("transmit loop starting type=%s", last_beacon_type)

        while not stop_event.is_set():
            refresh_level(conn=conn)
            now = time.time()
            now_dt = utc_now()
            tick_seconds = int(get_setting("BEACON_TICK_SECONDS", "2", conn=conn))

            beacon_type = _resolve_beacon_type(conn)
            if beacon_type != last_beacon_type:
                removed = delete_tx_schedule_other_kinds(conn, beacon_type)
                logger.info(
                    "BEACON_TYPE changed %s -> %s, cleared %d stale row(s)",
                    last_beacon_type, beacon_type, removed,
                )
                last_beacon_type = beacon_type

            enabled = get_setting("BEACON_ENABLED", BEACON_ENABLED_DEFAULT, conn=conn).lower() == "true"
            ntp_interval = int(get_setting("BEACON_NTP_CHECK_INTERVAL_SECONDS", "3600", conn=conn))
            reconcile_interval = int(
                get_setting("BEACON_CONTENT_READY_RECONCILE_INTERVAL_SECONDS", "30", conn=conn)
            )
            inter_tx_delay = float(get_setting("BEACON_INTER_TX_DELAY_SECONDS", "2", conn=conn))
            max_queued_age = int(
                get_setting(
                    "BEACON_MAX_QUEUED_AGE_SECONDS", BEACON_MAX_QUEUED_AGE_SECONDS_DEFAULT, conn=conn
                )
            )
            watermark_enabled = get_setting(
                "BEACON_WATERMARK_ENABLED", BEACON_WATERMARK_ENABLED_DEFAULT, conn=conn
            ).lower() == "true"
            watermark_interval = int(
                get_setting(
                    "BEACON_WATERMARK_INTERVAL_SECONDS", BEACON_WATERMARK_INTERVAL_SECONDS_DEFAULT, conn=conn
                )
            )
            ctx = {
                "callsign": get_setting("BEACON_CALLSIGN", conn=conn, env_fallback=False),
                "voice_template": get_setting("BEACON_VOICE_TEMPLATE", BEACON_VOICE_TEMPLATE_DEFAULT, conn=conn),
                "voice_max_chars": int(get_setting("BEACON_VOICE_MAX_CHARS", BEACON_VOICE_MAX_CHARS_DEFAULT, conn=conn)),
                "date_format": get_setting("BEACON_DATE_FORMAT", "%d-%m-%Y %H:%M", conn=conn),
                "destination": get_setting("BEACON_FRAME_DESTINATION", "NFO", conn=conn),
                "frame_prefix": get_setting("BEACON_FRAME_PREFIX", "", conn=conn) or "",
                "frame_suffix": get_setting("BEACON_FRAME_SUFFIX", "", conn=conn) or "",
                "voice_prefix": get_setting("BEACON_VOICE_PREFIX", BEACON_VOICE_PREFIX_DEFAULT, conn=conn) or "",
                "voice_suffix": get_setting("BEACON_VOICE_SUFFIX", BEACON_VOICE_SUFFIX_DEFAULT, conn=conn) or "",
                "voice_attention_tone": get_setting(
                    "BEACON_VOICE_ATTENTION_TONE", BEACON_VOICE_ATTENTION_TONE_DEFAULT, conn=conn
                ) or "",
                "wav_dir": _resolve_wav_dir(
                    get_setting("BEACON_TTS_WAV_DIR", "storage/beacon_tts", conn=conn)
                ),
                "tts_voice": get_setting("BEACON_TTS_VOICE", "es", conn=conn),
                "tts_engine": get_setting("BEACON_TTS_ENGINE", "piper", conn=conn),
                "tts_piper_model": get_setting(
                    "BEACON_TTS_PIPER_MODEL", "storage/piper_voices/es_MX-claude-high.onnx", conn=conn
                ),
                "tts_piper_binary": get_setting("BEACON_TTS_PIPER_BINARY", "piper", conn=conn),
                "gen_packets_binary": get_setting("BEACON_GEN_PACKETS_BINARY", "gen_packets", conn=conn),
                "frame_lead_silence_ms": int(get_setting("BEACON_FRAME_LEAD_SILENCE_MS", "250", conn=conn)),
                "watermark_voice_template": get_setting(
                    "BEACON_WATERMARK_VOICE_TEMPLATE", BEACON_WATERMARK_VOICE_TEMPLATE_DEFAULT, conn=conn
                ),
                "watermark_frame_template": get_setting(
                    "BEACON_WATERMARK_FRAME_TEMPLATE", BEACON_WATERMARK_FRAME_TEMPLATE_DEFAULT, conn=conn
                ),
                "manual_voice_template": get_setting(
                    "BEACON_MANUAL_VOICE_TEMPLATE", BEACON_MANUAL_VOICE_TEMPLATE_DEFAULT, conn=conn
                ),
                "wav_transmitter": _build_wav_transmitter(conn),
            }

            if now - last_ntp_check_at >= ntp_interval:
                _run_ntp_check(conn)
                last_ntp_check_at = now

            if now - last_reconcile_at >= reconcile_interval:
                reconciled = _reconcile_missed_content_ready(conn)
                if reconciled:
                    logger.warning("reconciled %d missed item.content_ready publish(es)", reconciled)
                last_reconcile_at = now

            if enabled and watermark_enabled and now - last_watermark_at >= watermark_interval:
                _transmit_watermark(conn, beacon_type, ctx, now_dt)
                last_watermark_at = now

            _write_heartbeat(conn, beacon_type)

            # Independent of BEACON_ENABLED: content that queued up while
            # transmit was off still ages out, so re-enabling doesn't replay
            # a stale overnight backlog.
            if beacon_type in KIND_TRANSMITTERS:
                _purge_stale_rows(conn, beacon_type, max_queued_age, now_dt)

            # Bound the rendered-WAV directory — checked ~hourly, independent
            # of BEACON_ENABLED (clips keep accruing from every airing).
            if now - last_prune_at >= 3600:
                _prune_old_wavs(
                    ctx["wav_dir"],
                    int(get_setting("BEACON_TTS_RETENTION_DAYS", BEACON_TTS_RETENTION_DAYS_DEFAULT, conn=conn)),
                    now,
                )
                last_prune_at = now

            if enabled and beacon_type in KIND_TRANSMITTERS:
                _drain_manual_tx(stop_event, conn, beacon_type, now_dt, ctx, inter_tx_delay)
                _drain_kind(stop_event, conn, beacon_type, now_dt, ctx, inter_tx_delay)

            wake_event.wait(timeout=tick_seconds)
            wake_event.clear()
    finally:
        conn.close()
    logger.info("transmit loop stopped")


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
    transmit_thread = threading.Thread(
        target=_run_transmit_loop,
        args=(stop_event, wake_event),
        name="beacon-transmit",
        daemon=True,
    )
    # Read-only tail of the SvxLink log that feeds the dashboard's "ON AIR"
    # indicator; dormant and harmless when the log isn't reachable.
    tx_monitor_thread = threading.Thread(
        target=tx_monitor.run_tx_monitor,
        args=(stop_event,),
        name="beacon-tx-monitor",
        daemon=True,
    )

    mqtt_thread.start()
    transmit_thread.start()
    tx_monitor_thread.start()

    mqtt_thread.join()
    transmit_thread.join()
    tx_monitor_thread.join()


if __name__ == "__main__":
    configure_logging("beacon")
    main()
