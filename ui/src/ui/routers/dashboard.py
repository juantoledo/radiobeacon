import json
import sqlite3
from datetime import datetime, timezone

from adapters.storage import count_manual_tx_by_kind, get_setting
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse
from starlette.responses import StreamingResponse

from .. import beacon_audio, queries, tx_stream
from ..audit_ack import resolve_ack_id
from ..current_user import get_current_user
from ..db import get_db
from ..setup import require_setup_complete
from ..templating import _parse_utc, templates
from .beacon import _status_context
from .manual_tx import frame_max_bytes, voice_max_chars

_EPOCH = datetime.min.replace(tzinfo=timezone.utc)

router = APIRouter(
    dependencies=[Depends(get_current_user), Depends(require_setup_complete)]
)

# NTP offset above this (seconds, absolute) flips the dashboard's Clock
# tile to a warning — mirrors beacon's own BEACON_NTP_MAX_OFFSET_SECONDS.
_NTP_WARN_DEFAULT = "2.0"


def _tx_stream_url(conn: sqlite3.Connection) -> str:
    """The operator's live transmission-audio stream URL, or "" when the
    dashboard's on-air audio (tx-audio.js + /dashboard/tx-stream) is off."""
    url = get_setting("UI_DASHBOARD_TX_STREAM_URL", "", conn=conn).strip()
    return url if url.startswith(("http://", "https://")) else ""


def _ingest_health(conn: sqlite3.Connection) -> dict:
    """Roll the per-source `adapter.fetch` audit rows up into one ingest
    status for the dashboard: newest fetch time across all sources, and
    whether the latest fetch of every source succeeded."""
    events = queries.last_adapter_fetch_events(conn)
    if not events:
        return {"state": "idle", "last_at": None, "sources": 0, "failing": [], "detail": []}
    failing = []
    last_at = None
    detail = []
    for source, event in sorted(events.items()):
        try:
            ok = json.loads(event["details"] or "{}").get("ok", True)
        except (ValueError, TypeError):
            ok = True
        if not ok:
            failing.append(source)
        if last_at is None or (event["recorded_at"] or "") > last_at:
            last_at = event["recorded_at"]
        detail.append({"source": source, "ok": ok, "at": event["recorded_at"]})
    return {
        "state": "error" if failing else "ok",
        "last_at": last_at,
        "sources": len(events),
        "failing": failing,
        "detail": detail,
    }


def _merge_items_feed(
    conn: sqlite3.Connection,
    items: list[sqlite3.Row],
    watermark_events: list[sqlite3.Row],
    manual_events: list[sqlite3.Row],
    limit: int,
) -> list[dict]:
    """Interleaves recent content items with recent watermark/manual beacon
    broadcasts, newest first, for the dashboard's Items tab. Neither a
    watermark nor a manual send has an `items` row of its own (no source/
    item_id) so they can't appear there on their own -- this gives each a
    plain entry (kind="watermark"/"manual") alongside real items (kind=
    "item"), all carrying an "at" timestamp for the merged sort.
    Watermark WAVs are deleted right after transmit (see
    beacon._transmit_watermark), so that entry never gets a play button;
    manual clips stick around under the normal retention sweep, so a manual
    entry gets one whenever its clip is still on disk.

    Sorting parses "at" with _parse_utc rather than comparing the raw
    strings: items store source_date_time as Python's offset-suffixed ISO
    8601 ("...T10:00:00+00:00") while audit_log's recorded_at is SQLite's
    offset-less datetime('now') ("...  10:00:00") -- compared as plain
    strings, the space sorts before "T" and silently shoves every
    watermark/manual entry to the bottom whenever an item shares its date."""
    entries = [{"kind": "item", "at": row["source_date_time"], **dict(row)} for row in items]
    entries += [
        {"kind": "watermark", "at": row["recorded_at"]} for row in watermark_events
    ]
    entries += [
        {
            "kind": "manual",
            "at": row["recorded_at"],
            "audio_url": beacon_audio.manual_tx_audio_url(conn, row["event_type"], row["details"]),
        }
        for row in manual_events
    ]
    entries.sort(key=lambda e: _parse_utc(e["at"]) or _EPOCH, reverse=True)
    return entries[:limit]


def _dashboard_context(conn: sqlite3.Connection, request: Request) -> dict:
    status_ctx = _status_context(conn)
    status = status_ctx["status"]

    ai_enabled = get_setting("ACTIONS_AI_ENABLED", "false", conn=conn).lower() == "true"
    watermark_enabled = (
        get_setting("BEACON_WATERMARK_ENABLED", "false", conn=conn).lower() == "true"
    )

    try:
        ntp_warn = abs(float(get_setting("BEACON_NTP_MAX_OFFSET_SECONDS", _NTP_WARN_DEFAULT, conn=conn)))
    except ValueError:
        ntp_warn = float(_NTP_WARN_DEFAULT)
    ntp_offset = None
    try:
        raw = status.get("last_ntp_offset_seconds")
        ntp_offset = None if raw is None else float(raw)
    except ValueError:
        ntp_offset = None

    recent_items = queries.recent_items(conn, limit=8)
    manual_pending = count_manual_tx_by_kind(conn)

    tx_counts = beacon_audio.item_transmission_counts(conn, recent_items)
    # Per-item clip list only for the (few) feed rows that have aired more
    # than once — one query each, and only for the visible 8 rows at most.
    item_transmissions = {
        key: beacon_audio.list_item_transmissions(conn, key[0], key[1])
        for key, n in tx_counts.items()
        if n > 1
    }
    items_feed = _merge_items_feed(
        conn,
        recent_items,
        queries.recent_watermark_transmits(conn, limit=8),
        queries.recent_manual_transmits(conn, limit=8),
        limit=8,
    )

    return {
        "manual_voice_max_chars": voice_max_chars(conn),
        "manual_frame_max_bytes": frame_max_bytes(conn),
        "manual_tx_pending": manual_pending,
        "manual_tx_pending_total": sum(manual_pending.values()),
        "last_manual_transmit_at": status.get("last_manual_transmit_at"),
        "counts": queries.dashboard_counts(conn),
        "sparkline": queries.items_sparkline(conn, days=14),
        "failed_24h": queries.failed_events_last_24h(conn, since_id=resolve_ack_id(request)),
        # Deliberately NOT ack-aware, unlike failed_24h above (which drops
        # to 0 once the admin dismisses the banner) — this is the KPI row's
        # persistent "how many, period" number, not a dismissible alert.
        "failed_events_24h": queries.failed_events_last_24h(conn),
        "recent_items": items_feed,
        "item_tx_counts": tx_counts,
        "item_transmissions": item_transmissions,
        "recent_audit_events": [
            {**dict(event), "audio_url": beacon_audio.manual_tx_audio_url(
                conn, event["event_type"], event["details"]
            )}
            for event in queries.recent_audit_events(conn, limit=8)
        ],
        "ingest": _ingest_health(conn),
        "ai_enabled": ai_enabled,
        "ai_last_run_at": queries.latest_event_at(conn, "action.ai.executed"),
        "watermark_enabled": watermark_enabled,
        "watermark_last_at": status.get("last_watermark_transmit_at"),
        "dashboard_tx_stream_configured": bool(_tx_stream_url(conn)),
        "beacon_callsign": get_setting("BEACON_CALLSIGN", "", conn=conn),
        "beacon_frequency": get_setting("BEACON_FREQUENCY", "", conn=conn),
        "beacon_started_at": status.get("process_started_at"),
        "last_voice_transmit_at": status.get("last_voice_transmit_at"),
        "last_frame_transmit_at": status.get("last_frame_transmit_at"),
        "ntp_offset": ntp_offset,
        "ntp_warn": ntp_warn,
        "ntp_checked_at": status.get("last_ntp_checked_at"),
        **status_ctx,
    }


@router.get("/")
def dashboard(request: Request, conn: sqlite3.Connection = Depends(get_db)):
    refresh_seconds = int(get_setting("UI_DASHBOARD_REFRESH_SECONDS", "5", conn=conn))
    context = {"refresh_seconds": refresh_seconds, **_dashboard_context(conn, request)}

    # The client-side auto-refresh asks for just the live region (smaller
    # payload, and it patches cells in place rather than reloading) — same
    # X-Auto-Refresh header dashboard-refresh.js has always sent.
    if request.headers.get("x-auto-refresh") or request.query_params.get("live"):
        return templates.TemplateResponse(request, "_dashboard_live.html", context)

    return templates.TemplateResponse(request, "dashboard.html", context)


@router.get("/dashboard/tx-state")
def tx_state(conn: sqlite3.Connection = Depends(get_db)):
    """Tiny JSON poll for the live "ON AIR" indicator (on-air.js) and the
    on-air audio auto-play (tx-audio.js, via on-air.js's rb:tx-state
    event). Both flags are False whenever beacon.tx_monitor isn't
    reporting, so both features stay dormant."""
    ctx = _status_context(conn)
    return JSONResponse(
        {
            "on_air": bool(ctx["on_air"]),
            "monitor_active": bool(ctx["tx_monitor_active"]),
        }
    )


@router.get("/dashboard/tx-stream")
async def tx_stream_route(conn: sqlite3.Connection = Depends(get_db)):
    """Reverse-proxy of the operator's live transmission-audio stream
    (UI_DASHBOARD_TX_STREAM_URL), fanned out from one upstream connection
    to every dashboard <audio> (tx-audio.js). 404 when no stream URL is
    configured."""
    url = _tx_stream_url(conn)
    if not url:
        raise HTTPException(status_code=404, detail="no transmission audio stream configured")
    q = await tx_stream.hub.subscribe(url)

    async def body():
        try:
            while True:
                chunk = await q.get()
                if chunk is None:
                    break
                yield chunk
        finally:
            tx_stream.hub.unsubscribe(q)

    return StreamingResponse(
        body(),
        media_type=tx_stream.hub.content_type,
        headers={"Cache-Control": "no-store"},
    )
