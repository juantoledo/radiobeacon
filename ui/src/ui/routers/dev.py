import math
import sqlite3
import uuid
from urllib.parse import urlencode

from adapters.storage import get_setting
from adapters.timeutil import utc_now
from dispatcher.override import reset_dispatch_state
from dispatcher.policy import DEFAULT_POLICY_NAME, list_policies
from fastapi import APIRouter, Depends, Form, HTTPException, Request
from starlette.responses import RedirectResponse

from .. import queries
from ..db import get_db, open_readonly_connection
from ..dev_ops import create_item, delete_item, update_item
from ..sql_guard import InvalidQuery, ensure_select_only
from ..templating import templates

ROW_LIMIT = 500


def _require_dev_tools_enabled(conn: sqlite3.Connection = Depends(get_db)) -> None:
    enabled = get_setting("UI_DEV_TOOLS_ENABLED", "true", conn=conn).lower() not in (
        "false",
        "0",
        "",
    )
    if not enabled:
        raise HTTPException(status_code=404, detail="not found")


router = APIRouter(dependencies=[Depends(_require_dev_tools_enabled)])


def _qs_without_page(request: Request) -> str:
    params = {k: v for k, v in request.query_params.items() if k != "page"}
    return urlencode(params)


@router.get("/dev")
def dev_hub_page(
    request: Request,
    source: str | None = None,
    type: str | None = None,
    dispatch_policy: str | None = None,
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
        dispatch_policy=dispatch_policy or None,
        event_key=event_key or None,
        q=q or None,
        limit=page_size,
        offset=offset,
    )
    total_pages = max(math.ceil(total / page_size), 1)

    return templates.TemplateResponse(
        request,
        "dev_hub.html",
        {
            "items": rows,
            "total": total,
            "page": page,
            "total_pages": total_pages,
            "qs": _qs_without_page(request),
            "base_url": "/dev",
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


@router.get("/dev/items/new")
def new_item_page(request: Request, conn: sqlite3.Connection = Depends(get_db)):
    return templates.TemplateResponse(
        request,
        "dev_item_form.html",
        {
            "item": None,
            "mode": "create",
            "policies": list_policies(conn),
            # Just a pre-filled suggestion (still a plain, editable text
            # input) — item_id has no format requirement, this only saves
            # having to invent a value by hand for the common case of a
            # one-off/test item where the exact id doesn't matter, and a
            # fresh uuid4 is collision-safe against anything already in
            # `items` without having to query for one.
            "suggested_item_id": str(uuid.uuid4()),
            # "Now" and the same default an item with no dispatch_policy
            # would resolve to anyway (dispatcher.policy.policy_for) —
            # both are just pre-filled, editable suggestions, not
            # requirements: a hand-created item usually represents
            # something happening now, on the least surprising policy.
            "suggested_source_date_time": utc_now().isoformat(),
            "suggested_dispatch_policy": DEFAULT_POLICY_NAME,
        },
    )


@router.post("/dev/items")
def create_item_action(
    source: str = Form(...),
    item_id: str = Form(...),
    extracted_title: str = Form(""),
    extracted_contents: str = Form(""),
    summary: str = Form(""),
    url: str = Form(""),
    event_key: str = Form(""),
    type: str = Form(""),
    subtype: str = Form(""),
    dispatch_policy: str = Form(""),
    source_date_time: str = Form(""),
    conn: sqlite3.Connection = Depends(get_db),
):
    try:
        create_item(
            conn,
            source=source,
            item_id=item_id,
            extracted_title=extracted_title or None,
            extracted_contents=extracted_contents or None,
            summary=summary or None,
            url=url or None,
            event_key=event_key or None,
            type_=type or None,
            subtype=subtype or None,
            dispatch_policy=dispatch_policy or None,
            source_date_time=source_date_time or None,
        )
    except sqlite3.IntegrityError:
        msg = f"an item with source={source!r} item_id={item_id!r} already exists"
        return RedirectResponse(url=f"/dev/items/new?{urlencode({'msg': msg})}", status_code=303)

    return RedirectResponse(
        url=f"/items/{source}/{item_id}?{urlencode({'msg': 'item created'})}", status_code=303
    )


@router.get("/dev/items/{source}/{item_id}/edit")
def edit_item_page(
    request: Request, source: str, item_id: str, conn: sqlite3.Connection = Depends(get_db)
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
        "dev_item_form.html",
        {
            "item": item,
            "mode": "edit",
            "policies": list_policies(conn),
            "default_consumer": default_consumer,
        },
    )


@router.post("/dev/items/{source}/{item_id}")
def update_item_action(
    source: str,
    item_id: str,
    extracted_title: str = Form(""),
    extracted_contents: str = Form(""),
    summary: str = Form(""),
    url: str = Form(""),
    event_key: str = Form(""),
    type: str = Form(""),
    subtype: str = Form(""),
    dispatch_policy: str = Form(""),
    source_date_time: str = Form(""),
    conn: sqlite3.Connection = Depends(get_db),
):
    updated = update_item(
        conn,
        source,
        item_id,
        extracted_title=extracted_title or None,
        extracted_contents=extracted_contents or None,
        summary=summary or None,
        url=url or None,
        event_key=event_key or None,
        type_=type or None,
        subtype=subtype or None,
        dispatch_policy=dispatch_policy or None,
        source_date_time=source_date_time or None,
    )
    msg = "item updated" if updated else "item not found — nothing updated"
    return RedirectResponse(
        url=f"/items/{source}/{item_id}?{urlencode({'msg': msg})}", status_code=303
    )


@router.post("/dev/items/{source}/{item_id}/delete")
def delete_item_action(source: str, item_id: str, conn: sqlite3.Connection = Depends(get_db)):
    deleted = delete_item(conn, source, item_id)
    msg = f"item {source}/{item_id} deleted" if deleted else "item not found"
    return RedirectResponse(url=f"/dev?{urlencode({'msg': msg})}", status_code=303)


@router.post("/dev/items/{source}/{item_id}/reset-dispatch")
def reset_dispatch_state_action(
    source: str, item_id: str, consumer: str = Form(...), conn: sqlite3.Connection = Depends(get_db)
):
    cleared = reset_dispatch_state(conn, consumer, source, item_id)
    msg = (
        f"dispatch state cleared for consumer={consumer}"
        if cleared
        else "nothing to clear for that consumer"
    )
    return RedirectResponse(
        url=f"/dev/items/{source}/{item_id}/edit?{urlencode({'msg': msg})}", status_code=303
    )


@router.get("/dev/sql")
def sql_runner_page(request: Request, q: str | None = None):
    error: str | None = None
    columns: list[str] = []
    rows: list[sqlite3.Row] = []
    truncated = False

    if q:
        try:
            validated = ensure_select_only(q)
        except InvalidQuery as exc:
            error = str(exc)
        else:
            conn = None
            try:
                conn = open_readonly_connection()
                cursor = conn.execute(validated)
                columns = [d[0] for d in cursor.description] if cursor.description else []
                fetched = cursor.fetchmany(ROW_LIMIT + 1)
                truncated = len(fetched) > ROW_LIMIT
                rows = fetched[:ROW_LIMIT]
            except sqlite3.Error as exc:
                error = str(exc)
            finally:
                if conn is not None:
                    conn.close()

    return templates.TemplateResponse(
        request,
        "dev_sql.html",
        {
            "q": q or "",
            "error": error,
            "columns": columns,
            "rows": rows,
            "truncated": truncated,
            "row_limit": ROW_LIMIT,
        },
    )
