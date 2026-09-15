"""The /setup first-run wizard: one page per ui.setup.SETUP_WIZARD_STEPS
entry, reusing config._build_fields/save_settings (and the same
_config_group_form_body.html partial the generic /config group-edit pages
use) for each step's curated field subset, plus a final review/confirm step
that marks the wizard complete.

Deliberately NOT gated by require_setup_complete — it's the escape hatch
require_setup_complete redirects everyone else to, so gating it too would
redirect-loop."""
import sqlite3

from adapters.storage import get_brand_asset
from fastapi import APIRouter, Depends, HTTPException, Request
from starlette.responses import RedirectResponse

from .. import branding
from ..config_catalog import spec_for_key
from ..current_user import require_role
from ..db import get_db
from ..setup import (
    FINISH_SLUG,
    SETUP_WIZARD_STEPS,
    is_setup_complete,
    mark_setup_complete,
    next_step_slug,
    prev_step_slug,
    step_for_slug,
    step_index,
)
from ..templating import templates
from .config import SECRET_SENTINEL, _build_fields, save_settings

router = APIRouter(dependencies=[Depends(require_role("admin"))])

# Computed once — which steps have no required field at all, so the
# progress list can mark them "optional" regardless of which step is
# currently being viewed (not just the active one).
_OPTIONAL_STEP_SLUGS = frozenset(
    step.slug
    for step in SETUP_WIZARD_STEPS
    if not any(spec_for_key(key).required for key in step.keys)
)


@router.get("/setup")
def setup_root_redirect():
    return RedirectResponse(url=f"/setup/{SETUP_WIZARD_STEPS[0].slug}", status_code=307)


def _step_context(
    slug: str, *, specs: list, fields: list[dict] | None, error: str | None
) -> dict:
    """Shared setup_wizard.html context builder for a real step's GET and its
    POST error-path re-render — the two only differ in whether `fields`
    holds stored values or the operator's just-submitted ones."""
    step_optional = not any(spec.required for spec in specs)
    return {
        "steps": SETUP_WIZARD_STEPS,
        "optional_slugs": _OPTIONAL_STEP_SLUGS,
        "step": step_for_slug(slug),
        "current_index": step_index(slug),
        "finish": False,
        "fields": fields,
        "sentinel": SECRET_SENTINEL,
        "error": error,
        "form_action": f"/setup/{slug}",
        "prev_url": f"/setup/{prev_step_slug(slug)}" if prev_step_slug(slug) else None,
        "step_optional": step_optional,
        # A plain GET link (not a POST) — "skip" means "don't save
        # anything, just move on," matching save_settings' own "blank
        # means no change" rule for every non-required field.
        "skip_url": f"/setup/{next_step_slug(slug)}" if step_optional else None,
    }


def _finish_context(conn: sqlite3.Connection) -> dict:
    return {
        "steps": SETUP_WIZARD_STEPS,
        "optional_slugs": _OPTIONAL_STEP_SLUGS,
        "step": None,
        "current_index": step_index(FINISH_SLUG),
        "finish": True,
        "already_complete": is_setup_complete(conn),
        "prev_url": f"/setup/{prev_step_slug(FINISH_SLUG)}",
        "error": None,
    }


@router.get("/setup/{slug}")
def setup_step_page(request: Request, slug: str, conn: sqlite3.Connection = Depends(get_db)):
    if slug == FINISH_SLUG:
        return templates.TemplateResponse(request, "setup_wizard.html", _finish_context(conn))

    step = step_for_slug(slug)
    if step is None:
        raise HTTPException(status_code=404, detail="setup step not found")

    if slug == "branding":
        ctx = _step_context(slug, specs=[], fields=[], error=None)
        ctx["asset"] = get_brand_asset(conn, "logo")
        return templates.TemplateResponse(request, "setup_wizard.html", ctx)

    specs = [spec_for_key(key) for key in step.keys]
    ctx = _step_context(slug, specs=specs, fields=_build_fields(conn, specs), error=None)
    return templates.TemplateResponse(request, "setup_wizard.html", ctx)


@router.post("/setup/{slug}")
async def setup_step_save_action(
    request: Request, slug: str, conn: sqlite3.Connection = Depends(get_db)
):
    if slug == FINISH_SLUG:
        mark_setup_complete(conn)
        return RedirectResponse(url="/", status_code=303)

    step = step_for_slug(slug)
    if step is None:
        raise HTTPException(status_code=404, detail="setup step not found")

    if slug == "branding":
        form = await request.form()
        upload = form.get("logo")
        if upload is None or isinstance(upload, str) or not upload.filename:
            # No file chosen -- same as clicking "skip", nothing to save.
            return RedirectResponse(url=f"/setup/{next_step_slug(slug)}", status_code=303)
        raw_bytes = await upload.read()
        try:
            branding.save_logo(conn, raw_bytes, actor="ui.setup")
        except branding.LogoUploadError as exc:
            ctx = _step_context(slug, specs=[], fields=[], error=str(exc))
            ctx["asset"] = get_brand_asset(conn, "logo")
            return templates.TemplateResponse(request, "setup_wizard.html", ctx, status_code=400)
        return RedirectResponse(url=f"/setup/{next_step_slug(slug)}", status_code=303)

    specs = [spec_for_key(key) for key in step.keys]
    form = await request.form()
    changed, error, fields = save_settings(conn, specs, form)
    if error:
        ctx = _step_context(slug, specs=specs, fields=fields, error=error)
        return templates.TemplateResponse(request, "setup_wizard.html", ctx, status_code=400)

    return RedirectResponse(url=f"/setup/{next_step_slug(slug)}", status_code=303)
