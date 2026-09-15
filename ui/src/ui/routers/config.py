import sqlite3
from urllib.parse import urlencode

from adapters.attention_tone import ToneSpecError, render_tone_wav
from adapters.storage import delete_setting, get_setting, list_settings, set_setting
from fastapi import APIRouter, Depends, HTTPException, Request
from starlette.responses import PlainTextResponse, RedirectResponse, Response

from ..ajax import ajax_ok, is_ajax
from ..config_catalog import (
    category_for_group,
    category_for_slug,
    category_slug,
    group_is_advanced,
    group_slug,
    is_multi_group_category,
    section_slug,
    sections_for_category,
    specs_for_group,
)
from .. import templating
from ..current_user import require_role
from ..db import get_db
from ..fragments import render_fragment
from ..templating import templates

router = APIRouter(dependencies=[Depends(require_role("admin"))])

# Rendered in a secret field when a DB row exists for that key — never the
# real value, and never varies with the stored value's length. Submitting a
# form with a field still at this sentinel (or left blank) is treated as
# "unchanged" by config_group_save_action, so the stored secret is never
# echoed back to the browser under any circumstance.
SECRET_SENTINEL = "•" * 8


def _category_sections_view(conn: sqlite3.Connection, category: str) -> list[dict]:
    """Section -> compact group-row list for one category's landing page — one
    row per group (not per key: the group's own edit form already shows every
    field's effective value), grouped under CATEGORY_LAYOUT's labelled,
    optionally-collapsed sections (see config_catalog.sections_for_category)."""
    overridden_keys = {row["key"] for row in list_settings(conn)}
    sections = []
    for section in sections_for_category(category):
        groups = []
        for group in section.groups:
            slug = group_slug(group)
            specs = specs_for_group(slug)
            groups.append(
                {
                    "name": group,
                    "slug": slug,
                    "count": len(specs),
                    "overridden": sum(1 for spec in specs if spec.key in overridden_keys),
                    "advanced": group_is_advanced(slug),
                    "has_wiring": any(spec.wiring for spec in specs),
                }
            )
        sections.append(
            {
                "label": section.label,
                "slug": section_slug(section.label),
                "collapsed": section.collapsed,
                "groups": groups,
            }
        )
    return sections


def _build_fields(conn: sqlite3.Connection, specs: list) -> list[dict]:
    """Per-field render context (effective value, secret masking,
    "overridden" flag, and this field's own reset URL) for an arbitrary list
    of specs — not necessarily a whole group's worth. Shared by
    build_group_form_context (a full group) and ui.setup's wizard steps
    (a curated cross-group subset), which is why reset_url is computed from
    each spec's *own* group rather than assumed to match a single caller-wide
    slug."""
    overrides = {row["key"]: row for row in list_settings(conn)}
    fields = []
    for spec in specs:
        row = overrides.get(spec.key)
        if spec.is_secret:
            value = SECRET_SENTINEL if row is not None else ""
        else:
            # Effective value (DB override -> env var -> catalog default) —
            # NOT just "the DB row or the catalog default", so a value
            # currently coming from an env var (not yet DB-overridden) still
            # shows correctly instead of being masked by the default.
            value = get_setting(
                spec.key, spec.default, conn=conn, env_fallback=spec.env_fallback
            ) or ""
        fields.append(
            {
                "spec": spec,
                "value": value,
                "overridden": row is not None,
                "reset_url": f"/config/{group_slug(spec.group)}/{spec.key}/reset",
            }
        )
    return fields


def save_settings(
    conn: sqlite3.Connection, specs: list, form
) -> tuple[int, str | None, list[dict] | None]:
    """Validates and writes `form` against `specs` — the group-agnostic core
    of what used to be config_group_save_action, extracted so ui.setup's
    wizard steps (a curated cross-group subset of specs, not a whole group)
    can reuse the exact same required-field / blank-means-no-change /
    secret-sentinel rules. Returns (changed_count, error, fields):
    - success: (changed_count, None, None)
    - required field left blank: (0, "Required: ...", fields) where `fields`
      carries every submitted value back (not the stored ones) so the
      caller can re-render the form exactly as the operator left it."""
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
                "reset_url": f"/config/{group_slug(spec.group)}/{spec.key}/reset",
            }
            for spec in specs
        ]
        return 0, f"Required: {', '.join(missing)}", fields

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
    return changed, None, None


def build_group_form_context(conn: sqlite3.Connection, group_slug: str) -> dict | None:
    """The config_group_form.html context for one settings group, or None if
    `group_slug` names no known group. Shared by the generic group edit page
    (config_tab_page) and ui.routers.rf_conf's SvxLink/Direwolf pages, which
    render the same group form plus a conf-file editor panel."""
    specs = specs_for_group(group_slug)
    if not specs:
        return None
    return {
        "group": specs[0].group,
        "slug": group_slug,
        "fields": _build_fields(conn, specs),
        "sentinel": SECRET_SENTINEL,
        "error": None,
        "form_action": f"/config/{group_slug}",
        "back_url": _group_back_url(specs[0].group, group_slug),
    }


def _group_back_url(group: str, slug: str) -> str | None:
    """Where a group's edit form's "back" crumb points. The Adapters —
    General group is special: its tab lives at /config/adapters (merged with
    the adapter-instance CRUD there, see ui.routers.adapters), not at a
    generic category page. A single-group category (Dispatcher, Secrets,
    Display, UI) has no separate listing page at all — its tab IS this form
    — so there's nothing to crumb back to."""
    if slug == "adapters-general":
        return "/config/adapters"
    category = category_for_group(group)
    if is_multi_group_category(category):
        return f"/config/{category_slug(category)}"
    return None


@router.get("/config")
def config_root_redirect():
    return RedirectResponse(url="/config/adapters", status_code=307)


# Registered before /config/{slug} so the literal path wins over the catch-all.
@router.get("/config/tone-preview")
def config_tone_preview(spec: str = ""):
    """Render BEACON_VOICE_ATTENTION_TONE to a standalone WAV so the config
    page can play it back before the operator saves. 400 (with a readable
    message) on a bad spec — the preview doubles as the validation surface."""
    try:
        wav = render_tone_wav(spec)
    except ToneSpecError as exc:
        return PlainTextResponse(str(exc), status_code=400)
    return Response(wav, media_type="audio/wav")


@router.get("/config/{slug}")
def config_tab_page(request: Request, slug: str, conn: sqlite3.Connection = Depends(get_db)):
    category = category_for_slug(slug)
    if category is not None and is_multi_group_category(category):
        return templates.TemplateResponse(
            request,
            "config_category.html",
            {
                "category": category,
                "sections": _category_sections_view(conn, category),
            },
        )

    ctx = build_group_form_context(conn, slug)
    if ctx is None:
        raise HTTPException(status_code=404, detail="settings group not found")
    return templates.TemplateResponse(request, "config_group_form.html", ctx)


@router.post("/config/{group}")
async def config_group_save_action(
    request: Request, group: str, conn: sqlite3.Connection = Depends(get_db)
):
    specs = specs_for_group(group)
    if not specs:
        raise HTTPException(status_code=404, detail="settings group not found")

    form = await request.form()
    changed, error, fields = save_settings(conn, specs, form)
    if error:
        return templates.TemplateResponse(
            request,
            "config_group_form.html",
            {
                "group": specs[0].group,
                "slug": group,
                "fields": fields,
                "sentinel": SECRET_SENTINEL,
                "error": error,
                "form_action": f"/config/{group}",
                "back_url": _group_back_url(specs[0].group, group),
            },
            status_code=400,
        )

    msg = f"{changed} setting(s) saved" if changed else "no changes"
    return RedirectResponse(
        url=f"/config/{group}?{urlencode({'msg': msg})}", status_code=303
    )


@router.post("/config/{group}/{key}/reset")
def config_setting_reset_action(
    request: Request, group: str, key: str, conn: sqlite3.Connection = Depends(get_db)
):
    specs = specs_for_group(group)
    if not any(spec.key == key for spec in specs):
        raise HTTPException(status_code=404, detail="setting not found in group")
    deleted = delete_setting(conn, key)
    msg = f"'{key}' reset to env/default" if deleted else f"'{key}' had no override"

    if is_ajax(request):
        # This request's per-request settings cache (see templating.py's
        # _request_setting) may already hold the pre-reset value if
        # anything read it earlier in this same request — drop it before
        # re-rendering the form body so the fragment reflects the reset,
        # not a stale cached read.
        invalidate = getattr(templating, "invalidate_request_settings", None)
        if invalidate is not None:
            invalidate(request)
        ctx = build_group_form_context(conn, group)
        fragment = render_fragment(request, "_config_group_form_body.html", ctx)
        return ajax_ok(msg, fragment=fragment, target="#config-group-body")

    return RedirectResponse(
        url=f"/config/{group}?{urlencode({'msg': msg})}", status_code=303
    )
