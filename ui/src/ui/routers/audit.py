import math
import sqlite3
from urllib.parse import urlencode

from adapters.storage import get_setting
from fastapi import APIRouter, Depends, Request

from .. import queries
from ..current_user import require_role
from ..db import get_db
from ..templating import templates

router = APIRouter(dependencies=[Depends(require_role("admin"))])


@router.get("/audit")
def audit_log_page(
    request: Request,
    event_type: str | None = None,
    source: str | None = None,
    item_id: str | None = None,
    page: int = 1,
    conn: sqlite3.Connection = Depends(get_db),
):
    page_size = int(get_setting("UI_PAGE_SIZE", "50", conn=conn))
    page = max(page, 1)
    offset = (page - 1) * page_size
    rows, total = queries.list_audit_log(
        conn,
        event_type=event_type or None,
        source=source or None,
        item_id=item_id or None,
        limit=page_size,
        offset=offset,
    )
    total_pages = max(math.ceil(total / page_size), 1)
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
