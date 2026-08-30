import sqlite3
from urllib.parse import urlencode

from adapters.transmit_policy import delete_policy, list_policies, set_policy
from fastapi import APIRouter, Depends, Form, HTTPException, Request
from starlette.responses import RedirectResponse

from .. import queries
from ..db import get_db
from ..templating import templates

router = APIRouter()


@router.get("/policies")
def policies_list_page(request: Request, conn: sqlite3.Connection = Depends(get_db)):
    return templates.TemplateResponse(
        request, "policies_list.html", {"policies": list_policies(conn)}
    )


@router.get("/policies/new")
def policy_new_page(request: Request):
    return templates.TemplateResponse(
        request, "policy_form.html", {"policy": None, "mode": "create"}
    )


@router.get("/policies/{name}/edit")
def policy_edit_page(request: Request, name: str, conn: sqlite3.Connection = Depends(get_db)):
    row = queries.get_policy_row(conn, name)
    if row is None:
        raise HTTPException(status_code=404, detail="policy not found")
    return templates.TemplateResponse(
        request, "policy_form.html", {"policy": row, "mode": "edit"}
    )


@router.post("/policies")
def policy_create_action(
    name: str = Form(...),
    repeat_times: int = Form(...),
    interval_seconds: int = Form(...),
    description: str = Form(""),
    conn: sqlite3.Connection = Depends(get_db),
):
    set_policy(conn, name, repeat_times, interval_seconds, description or None)
    msg = urlencode({"msg": f"policy '{name}' saved"})
    return RedirectResponse(url=f"/policies?{msg}", status_code=303)


@router.post("/policies/{name}/delete")
def policy_delete_action(name: str, conn: sqlite3.Connection = Depends(get_db)):
    deleted = delete_policy(conn, name)
    msg = f"policy '{name}' deleted" if deleted else f"policy '{name}' not found"
    return RedirectResponse(url=f"/policies?{urlencode({'msg': msg})}", status_code=303)
