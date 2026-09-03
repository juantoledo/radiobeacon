import json
import sqlite3

from adapters.storage import count_manual_tx_by_kind, get_setting
from fastapi import APIRouter, Depends, Request

from .. import beacon_audio, queries
from ..db import get_db
from ..templating import templates
from .beacon import _status_context
from .manual_tx import frame_max_bytes, voice_max_chars

router = APIRouter()

# NTP offset above this (seconds, absolute) flips the dashboard's Clock
# tile to a warning — mirrors beacon's own BEACON_NTP_MAX_OFFSET_SECONDS.
_NTP_WARN_DEFAULT = "2.0"


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


def _dashboard_context(conn: sqlite3.Connection) -> dict:
    status_ctx = _status_context(conn)
    status = status_ctx["status"]

    ai_enabled = get_setting("ACTIONS_AI_ENABLED", "false", conn=conn).lower() == "true"
    watermark_enabled = (
        get_setting("BEACON_WATERMARK_ENABLED", "false", conn=conn).lower() == "true"
    )
    dev_tools_enabled = get_setting("UI_DEV_TOOLS_ENABLED", "true", conn=conn).lower() not in (
        "false",
        "0",
        "",
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

    return {
        "manual_voice_max_chars": voice_max_chars(conn),
        "manual_frame_max_bytes": frame_max_bytes(conn),
        "manual_tx_pending": manual_pending,
        "manual_tx_pending_total": sum(manual_pending.values()),
        "manual_clips": beacon_audio.recent_manual_clips(conn),
        "last_manual_transmit_at": status.get("last_manual_transmit_at"),
        "counts": queries.dashboard_counts(conn),
        "sparkline": queries.items_sparkline(conn, days=14),
        "failed_24h": queries.failed_events_last_24h(conn),
        "recent_items": recent_items,
        "items_with_audio": beacon_audio.items_with_voice_clips(conn, recent_items),
        "recent_audit_events": queries.recent_audit_events(conn, limit=8),
        "ingest": _ingest_health(conn),
        "ai_enabled": ai_enabled,
        "ai_last_run_at": queries.latest_event_at(conn, "action.ai.executed"),
        "watermark_enabled": watermark_enabled,
        "watermark_last_at": status.get("last_watermark_transmit_at"),
        "dev_tools_enabled_now": dev_tools_enabled,
        "beacon_callsign": get_setting("BEACON_CALLSIGN", "", conn=conn),
        "beacon_started_at": status.get("process_started_at"),
        "last_voice_transmit_at": status.get("last_voice_transmit_at"),
        "last_frame_transmit_at": status.get("last_frame_transmit_at"),
        "voice_queue_depth": status_ctx["queue_depth"] if status_ctx["beacon_type"] != "frame"
        else _safe_int(status.get("voice_queue_depth")),
        "frame_queue_depth": status_ctx["queue_depth"] if status_ctx["beacon_type"] == "frame"
        else _safe_int(status.get("frame_queue_depth")),
        "ntp_offset": ntp_offset,
        "ntp_warn": ntp_warn,
        "ntp_checked_at": status.get("last_ntp_checked_at"),
        **status_ctx,
    }


def _safe_int(value: object) -> int:
    try:
        return int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return 0


@router.get("/")
def dashboard(request: Request, conn: sqlite3.Connection = Depends(get_db)):
    refresh_seconds = int(get_setting("UI_DASHBOARD_REFRESH_SECONDS", "5", conn=conn))
    context = {"refresh_seconds": refresh_seconds, **_dashboard_context(conn)}

    # The client-side auto-refresh asks for just the live region (smaller
    # payload, and it patches cells in place rather than reloading) — same
    # X-Auto-Refresh header dashboard-refresh.js has always sent.
    if request.headers.get("x-auto-refresh") or request.query_params.get("live"):
        return templates.TemplateResponse(request, "_dashboard_live.html", context)

    return templates.TemplateResponse(request, "dashboard.html", context)
