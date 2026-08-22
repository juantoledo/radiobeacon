import sqlite3
from urllib.parse import urlencode

from adapters.storage import delete_setting, get_setting, list_settings, set_setting
from fastapi import APIRouter, Depends, HTTPException, Request
from starlette.responses import RedirectResponse

from ..config_catalog import GROUPS, SETTINGS_CATALOG, group_slug, specs_for_group
from ..db import get_db
from ..templating import templates

router = APIRouter()

# Rendered in a secret field when a DB row exists for that key — never the
# real value, and never varies with the stored value's length. Submitting a
# form with a field still at this sentinel (or left blank) is treated as
# "unchanged" by config_group_save_action, so the stored secret is never
# echoed back to the browser under any circumstance.
SECRET_SENTINEL = "•" * 8


@router.get("/config")
def config_list_page(request: Request, conn: sqlite3.Connection = Depends(get_db)):
    overrides = {row["key"]: row for row in list_settings(conn)}
    groups = []
    for group in GROUPS:
        rows = []
        for spec in SETTINGS_CATALOG:
            if spec.group != group:
                continue
            row = overrides.get(spec.key)
            # Effective value (DB override -> env var -> catalog default) —
            # never resolved for secrets, which are masked unconditionally.
            effective = None if spec.is_secret else get_setting(spec.key, spec.default, conn=conn)
            rows.append({"spec": spec, "row": row, "effective": effective})
        groups.append({"name": group, "slug": group_slug(group), "rows": rows})
    return templates.TemplateResponse(
        request, "config_list.html", {"groups": groups, "sentinel": SECRET_SENTINEL}
    )


@router.get("/config/{group}")
def config_group_edit_page(
    request: Request, group: str, conn: sqlite3.Connection = Depends(get_db)
):
    specs = specs_for_group(group)
    if not specs:
        raise HTTPException(status_code=404, detail="settings group not found")
    overrides = {row["key"]: row for row in list_settings(conn)}

    fields = []
    for spec in specs:
        row = overrides.get(spec.key)
        if spec.is_secret:
            value = SECRET_SENTINEL if row is not None else ""
        else:
            # Effective value (DB override -> env var -> catalog default) —
            # NOT just "the DB row or the catalog default", so a value
            # currently coming from an env var (not yet DB-overridden)
            # still shows correctly instead of being masked by the default.
            value = get_setting(spec.key, spec.default, conn=conn) or ""
        fields.append({"spec": spec, "value": value, "overridden": row is not None})

    return templates.TemplateResponse(
        request,
        "config_group_form.html",
        {
            "group": specs[0].group,
            "slug": group,
            "fields": fields,
            "sentinel": SECRET_SENTINEL,
        },
    )


@router.post("/config/{group}")
async def config_group_save_action(
    request: Request, group: str, conn: sqlite3.Connection = Depends(get_db)
):
    specs = specs_for_group(group)
    if not specs:
        raise HTTPException(status_code=404, detail="settings group not found")

    form = await request.form()
    changed = 0
    for spec in specs:
        raw = form.get(spec.key)
        if raw is None:
            continue
        raw = raw.strip() if isinstance(raw, str) else raw
        # A blank field means "no explicit value provided" for every field,
        # not just secrets — get_setting() treats a stored empty string as
        # a real override (distinct from "no row"), so writing "" here
        # would silently force every unfilled field in the group to
        # resolve to "" instead of falling through to its env var/default.
        # Clearing an existing override is what the "Reset" button is for.
        if raw == "":
            continue
        if spec.is_secret and raw == SECRET_SENTINEL:
            continue  # unchanged — never overwrite a stored secret with the mask
        set_setting(conn, spec.key, raw, is_secret=spec.is_secret, actor="ui.config")
        changed += 1

    msg = f"{changed} setting(s) saved" if changed else "no changes"
    return RedirectResponse(
        url=f"/config/{group}?{urlencode({'msg': msg})}", status_code=303
    )


@router.post("/config/{group}/{key}/reset")
def config_setting_reset_action(
    group: str, key: str, conn: sqlite3.Connection = Depends(get_db)
):
    specs = specs_for_group(group)
    if not any(spec.key == key for spec in specs):
        raise HTTPException(status_code=404, detail="setting not found in group")
    deleted = delete_setting(conn, key)
    msg = f"'{key}' reset to env/default" if deleted else f"'{key}' had no override"
    return RedirectResponse(
        url=f"/config/{group}?{urlencode({'msg': msg})}", status_code=303
    )
