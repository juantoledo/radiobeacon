import math
import sqlite3
from urllib.parse import urlencode

from fastapi import APIRouter, Depends, Request

from .. import queries
from ..config import UI_PAGE_SIZE
from ..db import get_db
from ..templating import templates

router = APIRouter()


@router.get("/audit")
def audit_log_page(
    request: Request,
    event_type: str | None = None,
    source: str | None = None,
    item_id: str | None = None,
    page: int = 1,
    conn: sqlite3.Connection = Depends(get_db),
):
    page = max(page, 1)
    offset = (page - 1) * UI_PAGE_SIZE
    rows, total = queries.list_audit_log(
        conn,
        event_type=event_type or None,
        source=source or None,
        item_id=item_id or None,
        limit=UI_PAGE_SIZE,
        offset=offset,
    )
    total_pages = max(math.ceil(total / UI_PAGE_SIZE), 1)
    qs = urlencode(
        {k: v for k, v in request.query_params.items() if k != "page"}
    )

    return templates.TemplateResponse(
        request,
        "audit_log.html",
        {
            "events": rows,
            "total": total,
            "page": page,
            "total_pages": total_pages,
            "qs": qs,
            "base_url": "/audit",
            "filters": {
                "event_type": event_type or "",
                "source": source or "",
                "item_id": item_id or "",
            },
        },
    )
