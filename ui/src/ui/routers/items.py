import math
import sqlite3
from urllib.parse import urlencode

from adapters.storage import get_setting, schedule_retransmit
from dispatcher.mq_publisher import PUBLISHED_EVENT_TYPES
from dispatcher.override import override_item, rearm_item
from adapters.policy import list_policies
from fastapi import APIRouter, Depends, Form, HTTPException, Request
from starlette.responses import FileResponse, RedirectResponse

from .. import beacon_audio, queries
from ..beacon import is_beacon_configured
from ..current_user import require_role
from ..db import get_db
from ..templating import templates

# Read routes (list, detail, audio) are open to any authenticated account —
# the 'user' role is a read-only viewer. The mutating POST routes below
# re-assert require_role("admin") individually.
router = APIRouter(dependencies=[Depends(require_role("user"))])

_admin_only = [Depends(require_role("admin"))]


def _qs_without_page(request: Request) -> str:
    params = {k: v for k, v in request.query_params.items() if k != "page"}
    return urlencode(params)


@router.get("/items")
def list_items_page(
    request: Request,
    source: str | None = None,
    type: str | None = None,
    policy: str | None = None,
    event_key: str | None = None,
    q: str | None = None,
    page: int = 1,
    conn: sqlite3.Connection = Depends(get_db),
):
    page_size = int(get_setting("UI_PAGE_SIZE", "50", conn=conn))
    page = max(page, 1)
    offset = (page - 1) * page_size
    rows, total = queries.list_items(
        conn,
        source=source or None,
        type_=type or None,
        policy=policy or None,
        event_key=event_key or None,
        q=q or None,
        limit=page_size,
        offset=offset,
    )
    total_pages = max(math.ceil(total / page_size), 1)

    return templates.TemplateResponse(
        request,
        "items_list.html",
        {
            "items": rows,
            "total": total,
            "page": page,
            "total_pages": total_pages,
            "qs": _qs_without_page(request),
            "base_url": "/items",
            "filters": {
                "source": source or "",
                "type": type or "",
                "policy": policy or "",
                "event_key": event_key or "",
                "q": q or "",
            },
            "sources": queries.distinct_sources(conn),
            "types": queries.distinct_types(conn),
            "policies": list_policies(conn),
        },
    )


@router.get("/items/{source}/{item_id}")
def item_detail_page(
    request: Request,
    source: str,
    item_id: str,
    conn: sqlite3.Connection = Depends(get_db),
):
    item = queries.get_item(conn, source, item_id)
    if item is None:
        raise HTTPException(status_code=404, detail="item not found")

    default_consumer = get_setting(
        "UI_DEFAULT_CONSUMER_NAME",
        get_setting("DISPATCHER_CONSUMER_NAME", "log", conn=conn),
        conn=conn,
    )
    return templates.TemplateResponse(
        request,
        "item_detail.html",
        {
            "item": item,
            "chunks": queries.list_item_chunks(conn, source, item_id),
            "audit_events": queries.list_audit_log_for_item(conn, source, item_id),
            "transmissions": beacon_audio.list_item_transmissions(conn, source, item_id),
            "policies": list_policies(conn),
            "default_consumer": default_consumer,
            "replayable_event_types": PUBLISHED_EVENT_TYPES,
        },
    )


@router.get("/items/{source}/{item_id}/audio")
def item_audio(
    source: str,
    item_id: str,
    conn: sqlite3.Connection = Depends(get_db),
):
    """The WAV the beacon last rendered for this item's voice transmission,
    streamed for in-browser playback (the dashboard's per-item play
    button). 404 when nothing has been rendered for it yet."""
    clip = beacon_audio.latest_voice_clip(conn, source, item_id)
    if clip is None:
        raise HTTPException(status_code=404, detail="no rendered voice audio for this item")
    return FileResponse(clip, media_type="audio/wav", filename=clip.name)


@router.get("/items/{source}/{item_id}/audio/{name}")
def item_clip_audio(
    source: str,
    item_id: str,
    name: str,
    conn: sqlite3.Connection = Depends(get_db),
):
    """One specific rendered clip for this item (voice or frame), by
    filename — from the "Transmissions" list on the item page and the
    dashboard feed's per-airing play buttons. The name is validated against
    this item's own clip-name shape, so it can't escape the wav dir."""
    clip = beacon_audio.item_clip_path(conn, source, item_id, name)
    if clip is None:
        raise HTTPException(status_code=404, detail="clip not found")
    return FileResponse(clip, media_type="audio/wav", filename=clip.name)


@router.post("/items/{source}/{item_id}/override", dependencies=_admin_only)
def override_item_action(
    source: str,
    item_id: str,
    policy: str = Form(...),
    conn: sqlite3.Connection = Depends(get_db),
):
    if not is_beacon_configured(conn):
        error = "beacon identity not configured — set it up before re-pointing the Policy"
        return RedirectResponse(
            url=f"/items/{source}/{item_id}?{urlencode({'error': error})}", status_code=303
        )
    updated = override_item(conn, source, item_id, policy=policy)
    msg = "policy updated" if updated else "item not found — nothing updated"
    return RedirectResponse(
        url=f"/items/{source}/{item_id}?{urlencode({'msg': msg})}", status_code=303
    )


@router.post("/items/{source}/{item_id}/rearm", dependencies=_admin_only)
def rearm_item_action(
    source: str,
    item_id: str,
    consumer: str = Form(...),
    conn: sqlite3.Connection = Depends(get_db),
):
    if not is_beacon_configured(conn):
        error = "beacon identity not configured — set it up before re-arming"
        return RedirectResponse(
            url=f"/items/{source}/{item_id}?{urlencode({'error': error})}", status_code=303
        )
    rearmed = rearm_item(conn, consumer, source, item_id)
    msg = (
        f"re-armed for consumer={consumer}"
        if rearmed
        else "not re-armed — item doesn't exist or is already in-flight"
    )
    return RedirectResponse(
        url=f"/items/{source}/{item_id}?{urlencode({'msg': msg})}", status_code=303
    )


@router.post("/items/{source}/{item_id}/replay", dependencies=_admin_only)
def replay_audit_event_action(
    source: str,
    item_id: str,
    event_type: str = Form(...),
    consumer: str = Form(...),
    conn: sqlite3.Connection = Depends(get_db),
):
    if not is_beacon_configured(conn):
        error = "beacon identity not configured — set it up before replaying"
        return RedirectResponse(
            url=f"/items/{source}/{item_id}?{urlencode({'error': error})}", status_code=303
        )
    if event_type not in PUBLISHED_EVENT_TYPES:
        error = f"'{event_type}' isn't replayable — nothing downstream reacts to it"
        return RedirectResponse(
            url=f"/items/{source}/{item_id}?{urlencode({'error': error})}", status_code=303
        )
    rearmed = rearm_item(conn, consumer, source, item_id)
    key = "msg" if rearmed else "error"
    msg = (
        f"replayed {event_type} — re-armed for consumer={consumer}"
        if rearmed
        else "not re-armed — item doesn't exist or is already in-flight"
    )
    return RedirectResponse(
        url=f"/items/{source}/{item_id}?{urlencode({key: msg})}", status_code=303
    )


@router.post("/items/{source}/{item_id}/retransmit", dependencies=_admin_only)
def retransmit_item_action(
    source: str,
    item_id: str,
    conn: sqlite3.Connection = Depends(get_db),
):
    if not is_beacon_configured(conn):
        error = "beacon identity not configured — set it up before retransmitting"
        return RedirectResponse(
            url=f"/items/{source}/{item_id}?{urlencode({'error': error})}", status_code=303
        )
    result = schedule_retransmit(conn, source, item_id)
    if result["scheduled"] == 0:
        key, msg = "error", "not retransmitted — item not found"
    else:
        key, msg = "msg", (
            f"retransmit scheduled ({result['kind']}, {result['scheduled']} unit(s)) "
            "— beacon will send it on its next tick"
        )
    return RedirectResponse(
        url=f"/items/{source}/{item_id}?{urlencode({key: msg})}", status_code=303
    )
