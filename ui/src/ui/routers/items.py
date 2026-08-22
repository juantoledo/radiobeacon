import math
import sqlite3
from urllib.parse import urlencode

from dispatcher.override import override_item, rearm_item
from dispatcher.policy import list_policies
from fastapi import APIRouter, Depends, Form, HTTPException, Request
from starlette.responses import RedirectResponse

from .. import queries
from ..beacon import is_beacon_configured
from ..config import UI_DEFAULT_CONSUMER_NAME, UI_PAGE_SIZE
from ..db import get_db
from ..templating import templates

router = APIRouter()


def _qs_without_page(request: Request) -> str:
    params = {k: v for k, v in request.query_params.items() if k != "page"}
    return urlencode(params)


@router.get("/items")
def list_items_page(
    request: Request,
    source: str | None = None,
    type: str | None = None,
    dispatch_policy: str | None = None,
    event_key: str | None = None,
    q: str | None = None,
    page: int = 1,
    conn: sqlite3.Connection = Depends(get_db),
):
    page = max(page, 1)
    offset = (page - 1) * UI_PAGE_SIZE
    rows, total = queries.list_items(
        conn,
        source=source or None,
        type_=type or None,
        dispatch_policy=dispatch_policy or None,
        event_key=event_key or None,
        q=q or None,
        limit=UI_PAGE_SIZE,
        offset=offset,
    )
    total_pages = max(math.ceil(total / UI_PAGE_SIZE), 1)

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
                "dispatch_policy": dispatch_policy or "",
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

    return templates.TemplateResponse(
        request,
        "item_detail.html",
        {
            "item": item,
            "chunks": queries.list_item_chunks(conn, source, item_id),
            "audit_events": queries.list_audit_log_for_item(conn, source, item_id),
            "policies": list_policies(conn),
            "default_consumer": UI_DEFAULT_CONSUMER_NAME,
        },
    )


@router.post("/items/{source}/{item_id}/override")
def override_item_action(
    source: str,
    item_id: str,
    dispatch_policy: str = Form(...),
    conn: sqlite3.Connection = Depends(get_db),
):
    if not is_beacon_configured(conn):
        error = "beacon identity not configured — set it up before overriding dispatch_policy"
        return RedirectResponse(
            url=f"/items/{source}/{item_id}?{urlencode({'error': error})}", status_code=303
        )
    updated = override_item(conn, source, item_id, dispatch_policy=dispatch_policy)
    msg = "dispatch_policy updated" if updated else "item not found — nothing updated"
    return RedirectResponse(
        url=f"/items/{source}/{item_id}?{urlencode({'msg': msg})}", status_code=303
    )


@router.post("/items/{source}/{item_id}/rearm")
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
