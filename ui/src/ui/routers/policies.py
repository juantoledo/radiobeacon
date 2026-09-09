import sqlite3
from urllib.parse import urlencode

from adapters.policy import (
    describe_policy,
    delete_policy,
    list_policies,
    policy_reference_count,
    resolve_policy,
    set_policy,
)
from fastapi import APIRouter, Depends, Form, HTTPException, Request
from starlette.responses import RedirectResponse

from .. import queries
from ..current_user import require_role
from ..db import get_db
from ..templating import templates

router = APIRouter(dependencies=[Depends(require_role("admin"))])


def _cron_gap_warning(conn, fetch_kind, fetch_cron, transmit_kind, transmit_cron):
    """Warn when a cron fetch + cron transmit combination leaves items
    stale or never aired — every transmit occurrence after a fetch falls
    after the *next* fetch, or all transmit occurrences precede the fetch."""
    if fetch_kind != "cron" or transmit_kind != "cron":
        return None
    try:
        from datetime import timedelta

        from adapters.cron import next_fire_after
        from adapters.timeutil import utc_now

        now = utc_now()
        f1 = next_fire_after(fetch_cron, now, conn=conn)
        if f1 is None:
            return None
        f2 = next_fire_after(fetch_cron, f1, conn=conn)
        t_after_f1 = next_fire_after(transmit_cron, f1, conn=conn)
        if t_after_f1 is None or (f2 is not None and t_after_f1 >= f2):
            return (
                "items generated on the fetch schedule may be superseded by the next "
                "fetch before they ever air — check the transmit times fall between fetches"
            )
    except Exception:
        return None
    return None


@router.get("/policies", include_in_schema=False)
def policies_legacy_redirect():
    """The Policies page moved under /config — keep old bookmarks working."""
    return RedirectResponse(url="/config/policies", status_code=307)


@router.get("/config/policies")
def policies_list_page(request: Request, conn: sqlite3.Connection = Depends(get_db)):
    rows = list_policies(conn)
    policies = [
        {"row": r, "summary": describe_policy(resolve_policy(conn, r.name))} for r in rows
    ]
    return templates.TemplateResponse(
        request, "policies_list.html", {"policies": policies}
    )


@router.get("/config/policies/new")
def policy_new_page(request: Request):
    return templates.TemplateResponse(
        request, "policy_form.html", {"policy": None, "mode": "create"}
    )


@router.get("/config/policies/{name}/edit")
def policy_edit_page(request: Request, name: str, conn: sqlite3.Connection = Depends(get_db)):
    row = queries.get_policy_row(conn, name)
    if row is None:
        raise HTTPException(status_code=404, detail="policy not found")
    return templates.TemplateResponse(
        request, "policy_form.html", {"policy": row, "mode": "edit"}
    )


@router.post("/config/policies")
def policy_create_action(
    name: str = Form(...),
    fetch_kind: str = Form("interval"),
    fetch_interval_seconds: str = Form(""),
    fetch_cron: str = Form(""),
    transmit_kind: str = Form("once"),
    transmit_count: int = Form(1),
    transmit_interval_seconds: int = Form(0),
    transmit_cron: str = Form(""),
    description: str = Form(""),
    conn: sqlite3.Connection = Depends(get_db),
):
    try:
        set_policy(
            conn,
            name,
            fetch_kind=fetch_kind,
            fetch_interval_seconds=int(fetch_interval_seconds) if fetch_interval_seconds else None,
            fetch_cron=fetch_cron or None,
            transmit_kind=transmit_kind,
            transmit_count=transmit_count,
            transmit_interval_seconds=transmit_interval_seconds,
            transmit_cron=transmit_cron or None,
            description=description or None,
        )
    except ValueError as e:
        return RedirectResponse(
            url=f"/config/policies?{urlencode({'msg': f'not saved: {e}'})}", status_code=303
        )
    msg = f"policy '{name}' saved"
    warning = _cron_gap_warning(conn, fetch_kind, fetch_cron, transmit_kind, transmit_cron)
    if warning:
        msg = f"{msg} — {warning}"
    return RedirectResponse(url=f"/config/policies?{urlencode({'msg': msg})}", status_code=303)


@router.post("/config/policies/{name}/delete")
def policy_delete_action(name: str, conn: sqlite3.Connection = Depends(get_db)):
    refs = policy_reference_count(conn, name)
    deleted = delete_policy(conn, name)
    if not deleted:
        msg = f"policy '{name}' not found"
    elif refs["adapters"] or refs["items"]:
        msg = (
            f"policy '{name}' deleted — {refs['adapters']} adapter(s) and {refs['items']} "
            f"item(s) still named it and now fall back to 'default'"
        )
    else:
        msg = f"policy '{name}' deleted"
    return RedirectResponse(url=f"/config/policies?{urlencode({'msg': msg})}", status_code=303)
