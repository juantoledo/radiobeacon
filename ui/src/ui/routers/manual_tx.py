"""The dashboard's "Transmit now" action — queue one operator-typed
message for the beacon to put on air on its next tick.

Like every other write action in this auth-less app it only writes DB
state (adapters.storage.enqueue_manual_tx); the beacon process does the
actual TTS / AX.25 render and keys the radio (beacon.__main__._drain_manual_tx).
The message is sent once and dropped — there's no Policy and no
retry. It only goes on air while BEACON_ENABLED is on; queued while it's
off, sent when the operator turns it back on.
"""
import sqlite3
from urllib.parse import urlencode

from adapters.ax25 import max_frame_content_bytes
from adapters.beacon_defaults import BEACON_TYPE_DEFAULT, BEACON_VOICE_MAX_CHARS_DEFAULT
from adapters.storage import enqueue_manual_tx, get_setting
from fastapi import APIRouter, Depends, Form, HTTPException, Request
from starlette.responses import FileResponse, RedirectResponse

from .. import beacon_audio
from ..ajax import ajax_error, ajax_ok, is_ajax
from ..beacon import is_beacon_configured
from ..current_user import require_role
from ..db import get_db
from ..fragments import render_dashboard_live

router = APIRouter(dependencies=[Depends(require_role("admin"))])

_KINDS = ("voice", "frame")


def voice_max_chars(conn: sqlite3.Connection) -> int:
    try:
        return int(get_setting("BEACON_VOICE_MAX_CHARS", BEACON_VOICE_MAX_CHARS_DEFAULT, conn=conn))
    except ValueError:
        return int(BEACON_VOICE_MAX_CHARS_DEFAULT)


def frame_max_bytes(conn: sqlite3.Connection) -> int:
    """UTF-8 bytes left for the message once the AX.25
    "{callsign}>{destination}:" header is accounted for — mirrors
    beacon.formatters.format_frame's own limit (manual frames carry no
    prefix/suffix). Floored at 0."""
    callsign = get_setting("BEACON_CALLSIGN", "", conn=conn)
    destination = get_setting("BEACON_FRAME_DESTINATION", "NFO", conn=conn)
    return max(0, max_frame_content_bytes(callsign=callsign, destination=destination))


def _redirect(key: str, message: str) -> RedirectResponse:
    return RedirectResponse(url=f"/?{urlencode({key: message})}", status_code=303)


@router.post("/dashboard/transmit")
def dashboard_transmit(
    request: Request,
    kind: str = Form(...),
    text: str = Form(...),
    conn: sqlite3.Connection = Depends(get_db),
):
    def _error(message: str):
        if is_ajax(request):
            return ajax_error(message)
        return _redirect("error", message)

    if not is_beacon_configured(conn):
        return _error("beacon identity not configured — set it up before transmitting")

    kind = (kind or "").strip().lower()
    if kind not in _KINDS:
        return _error("pick voice or frame")

    text = (text or "").strip()
    if not text:
        return _error("nothing to transmit — the message is empty")

    if kind == "voice":
        limit = voice_max_chars(conn)
        if len(text) > limit:
            return _error(f"message is {len(text)} chars — the voice limit is {limit}")
    else:
        limit = frame_max_bytes(conn)
        size = len(text.encode("utf-8"))
        if size > limit:
            return _error(f"message is {size} bytes — the frame limit is {limit}")

    enqueue_manual_tx(conn, kind=kind, text=text, actor="ui.dashboard")
    msg = f"{kind} message queued — the beacon sends it on its next tick"
    if is_ajax(request):
        return ajax_ok(msg, fragment=render_dashboard_live(request, conn))
    return _redirect("msg", msg)


@router.get("/dashboard/manual-audio/{name}")
def manual_audio(name: str, conn: sqlite3.Connection = Depends(get_db)):
    """Streams one rendered manual-transmission clip for the dashboard's
    'Recent manual transmissions' play buttons. 404 for an unknown name
    (or one that isn't a manual-<id>-<ts>.wav)."""
    clip = beacon_audio.manual_clip_path(conn, name)
    if clip is None:
        raise HTTPException(status_code=404, detail="no such manual transmission clip")
    return FileResponse(clip, media_type="audio/wav", filename=clip.name)
