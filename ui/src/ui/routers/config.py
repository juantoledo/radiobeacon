import sqlite3
from urllib.parse import urlencode

from adapters.storage import delete_setting, get_setting, list_settings, set_setting
from fastapi import APIRouter, Depends, HTTPException, Request
from starlette.responses import RedirectResponse

from ..config_catalog import (
    CATEGORIES,
    SETTINGS_CATALOG,
    category_slug,
    group_is_advanced,
    group_slug,
    groups_for_category,
    is_advanced,
    specs_for_group,
)
from ..db import get_db
from ..templating import templates

router = APIRouter()

# Rendered in a secret field when a DB row exists for that key — never the
# real value, and never varies with the stored value's length. Submitting a
# form with a field still at this sentinel (or left blank) is treated as
# "unchanged" by config_group_save_action, so the stored secret is never
# echoed back to the browser under any circumstance.
SECRET_SENTINEL = "•" * 8


def _catalog_view(conn: sqlite3.Connection, *, advanced: bool) -> list[dict]:
    """Category -> group -> row tree for the /config list, filtered to one
    tier: the friendly "Settings" tab (advanced=False) or the "Advanced" tab
    (advanced=True). Groups and categories with no spec in the tier are
    dropped so neither tab shows an empty section."""
    overrides = {row["key"]: row for row in list_settings(conn)}
    categories = []
    for category in CATEGORIES:
        cat_groups = []
        for group in groups_for_category(category):
            rows = []
            for spec in SETTINGS_CATALOG:
                if spec.group != group or is_advanced(spec) != advanced:
                    continue
                row = overrides.get(spec.key)
                # Effective value (DB override -> env var -> catalog
                # default, or DB override -> catalog default only when
                # spec.env_fallback is False) — never resolved for
                # secrets, which are masked unconditionally.
                effective = (
                    None
                    if spec.is_secret
                    else get_setting(
                        spec.key, spec.default, conn=conn, env_fallback=spec.env_fallback
                    )
                )
                rows.append({"spec": spec, "row": row, "effective": effective})
            if not rows:
                continue
            cat_groups.append({"name": group, "slug": group_slug(group), "rows": rows})
        if cat_groups:
            categories.append(
                {"name": category, "slug": category_slug(category), "groups": cat_groups}
            )
    return categories


@router.get("/config")
def config_list_page(request: Request, conn: sqlite3.Connection = Depends(get_db)):
    return templates.TemplateResponse(
        request,
        "config_list.html",
        {
            "tier": "settings",
            "categories": _catalog_view(conn, advanced=False),
            "sentinel": SECRET_SENTINEL,
        },
    )


# Declared before /config/{group} so "advanced" is never captured as a group
# slug (Starlette matches routes in registration order).
@router.get("/config/advanced")
def config_advanced_list_page(request: Request, conn: sqlite3.Connection = Depends(get_db)):
    return templates.TemplateResponse(
        request,
        "config_list.html",
        {
            "tier": "advanced",
            "categories": _catalog_view(conn, advanced=True),
            "sentinel": SECRET_SENTINEL,
        },
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
            # env_fallback=spec.env_fallback so a DB-only group (beacon
            # identity) never picks up a stray env var of the same name.
            value = get_setting(spec.key, spec.default, conn=conn, env_fallback=spec.env_fallback) or ""
        fields.append({"spec": spec, "value": value, "overridden": row is not None})

    return templates.TemplateResponse(
        request,
        "config_group_form.html",
        {
            "group": specs[0].group,
            "slug": group,
            "fields": fields,
            "sentinel": SECRET_SENTINEL,
            "error": None,
            "back_url": "/config/advanced" if group_is_advanced(group) else "/config",
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
    raw_values: dict[str, str] = {}
    for spec in specs:
        raw = form.get(spec.key)
        raw_values[spec.key] = raw.strip() if isinstance(raw, str) else (raw or "")

    # required=True fields (currently just beacon identity) reject the
    # WHOLE submission if any of them is blank, re-rendering with every
    # submitted value preserved — unlike the general "blank means no
    # change" rule below, which doesn't apply to these fields at all.
    missing = [spec.label for spec in specs if spec.required and not raw_values[spec.key]]
    if missing:
        overrides = {row["key"]: row for row in list_settings(conn)}
        fields = [
            {
                "spec": spec,
                "value": raw_values[spec.key],
                "overridden": overrides.get(spec.key) is not None,
            }
            for spec in specs
        ]
        return templates.TemplateResponse(
            request,
            "config_group_form.html",
            {
                "group": specs[0].group,
                "slug": group,
                "fields": fields,
                "sentinel": SECRET_SENTINEL,
                "error": f"Required: {', '.join(missing)}",
                "back_url": "/config/advanced" if group_is_advanced(group) else "/config",
            },
            status_code=400,
        )

    changed = 0
    for spec in specs:
        raw = raw_values[spec.key]
        # A blank field means "no explicit value provided" for every
        # non-required field — get_setting() treats a stored empty string
        # as a real override (distinct from "no row"), so writing "" here
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
