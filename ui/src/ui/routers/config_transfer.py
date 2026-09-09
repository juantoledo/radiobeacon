"""GET/POST /config/import-export — the UI half of adapters.config_transfer,
covering the whole DB-backed config (settings + policies + sources
+ adapter_instances) in one JSON snapshot. Secrets are always excluded on
export; import is merge/upsert-only (never deletes) and goes through the
exact same import_config() the CLI pair (dispatcher/export_config.py /
import_config.py) uses — see that module's docstring for the shared shape
and per-record validation rules.

Registered in app.py BEFORE config.router, same reason already documented
there for rf_conf.router and adapters.router: literal paths under
/config/import-export/* would otherwise be swallowed by config.router's
GET /config/{slug} catch-all.

Preview -> apply carries the exact uploaded JSON text forward as a hidden
form field rather than a server-side token-keyed stash — the login
session (ui.current_user) is for auth only, not a place to park scratch
request state, and even the largest CUSTOM adapter snippet in this repo
(~7 KB) is trivial to resubmit once."""
import json
import sqlite3
from typing import Any
from urllib.parse import urlencode

from adapters.config_transfer import ImportSummary, build_export, import_config
from adapters.storage import get_setting
from adapters.timeutil import utc_now
from fastapi import APIRouter, Depends, File, Form, Request, UploadFile
from starlette.responses import RedirectResponse, Response

from ..current_user import require_role
from ..db import get_db
from ..templating import templates

router = APIRouter(dependencies=[Depends(require_role("admin"))])

# Truncated to a skim-able length in the CUSTOM-code review banner — the
# full snippet is still in the hidden config_json field and gets stored
# verbatim on confirm, this is just what's shown for the operator's
# "have I actually read this" checkbox.
_CODE_PREVIEW_CHARS = 400


def _dev_tools_enabled(conn: sqlite3.Connection) -> bool:
    # Same one-liner as ui.routers.adapters._dev_tools_enabled /
    # ui.routers.dev._require_dev_tools_enabled / ui.templating's Jinja
    # global — already duplicated that many times in this codebase; not
    # introducing a new pattern by having a fourth copy here.
    return get_setting("UI_DEV_TOOLS_ENABLED", "true", conn=conn).lower() not in (
        "false",
        "0",
        "",
    )


def _custom_adapter_previews(data: dict[str, Any], summary: ImportSummary) -> list[dict]:
    """CUSTOM-type adapter_instances entries from the raw uploaded data that
    import_config actually accepted (not skipped for some other reason,
    e.g. a missing 'config') — what a confirm click would really store and
    start exec()'ing on the adapter's next poll. Only meaningful when
    allow_custom_code was True; the caller skips calling this otherwise."""
    accepted_sources = {
        r.key
        for r in summary.results
        if r.section == "adapter_instances" and r.action != "skipped"
    }
    previews = []
    for entry in data.get("adapter_instances") or []:
        if not isinstance(entry, dict) or entry.get("adapter_type") != "custom":
            continue
        source = entry.get("source")
        if source not in accepted_sources:
            continue
        config = entry.get("config")
        code = config.get("code") if isinstance(config, dict) else None
        code = code if isinstance(code, str) else ""
        truncated = code[:_CODE_PREVIEW_CHARS]
        previews.append(
            {
                "source": source,
                "code_preview": truncated,
                "truncated": len(code) > _CODE_PREVIEW_CHARS,
            }
        )
    return previews


def _skipped_custom_count(data: dict[str, Any]) -> int:
    """How many CUSTOM-type entries the file carries, full stop — used only
    when dev tools are OFF, where every one of them is uniformly skipped
    for that one reason (see adapters.config_transfer's CUSTOM gate)."""
    return sum(
        1
        for e in (data.get("adapter_instances") or [])
        if isinstance(e, dict) and e.get("adapter_type") == "custom"
    )


def _needs_overwrite_confirm(summary: ImportSummary) -> bool:
    return any(c["created"] + c["updated"] > 0 for c in summary.counts().values())


def _preview_context(
    *,
    summary: ImportSummary,
    data: dict[str, Any],
    raw_text: str,
    dev_tools_enabled: bool,
    error: str | None = None,
) -> dict:
    custom_previews = _custom_adapter_previews(data, summary) if dev_tools_enabled else []
    skipped_custom = 0 if dev_tools_enabled else _skipped_custom_count(data)
    return {
        "summary": summary,
        "raw_text": raw_text,
        "custom_previews": custom_previews,
        "skipped_custom_count": skipped_custom,
        "needs_overwrite_confirm": _needs_overwrite_confirm(summary),
        "error": error,
    }


@router.get("/config/import-export")
def config_import_export_page(request: Request):
    return templates.TemplateResponse(request, "config_import_export.html", {})


@router.get("/config/import-export/download")
def config_export_download(conn: sqlite3.Connection = Depends(get_db)):
    export = build_export(conn, actor="ui.config.export")
    body = json.dumps(export, indent=2) + "\n"
    filename = f"radiobeacon-config-{utc_now().strftime('%Y%m%dT%H%M%SZ')}.json"
    return Response(
        body,
        media_type="application/json",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


def _reject_to_landing(message: str) -> RedirectResponse:
    return RedirectResponse(
        url=f"/config/import-export?{urlencode({'error': message})}", status_code=303
    )


@router.post("/config/import-export/preview")
async def config_import_preview_action(
    request: Request,
    upload: UploadFile = File(...),
    conn: sqlite3.Connection = Depends(get_db),
):
    raw_bytes = await upload.read()
    try:
        raw_text = raw_bytes.decode("utf-8")
    except UnicodeDecodeError:
        return _reject_to_landing("the uploaded file is not valid UTF-8 text")
    try:
        data = json.loads(raw_text)
    except json.JSONDecodeError as e:
        return _reject_to_landing(f"the uploaded file is not valid JSON ({e})")
    if not isinstance(data, dict):
        return _reject_to_landing("the uploaded file's top level must be a JSON object")

    dev_tools_enabled = _dev_tools_enabled(conn)
    try:
        summary = import_config(
            conn, data, allow_custom_code=dev_tools_enabled, actor="ui.config.import",
            dry_run=True,
        )
    except ValueError as e:
        return _reject_to_landing(str(e))

    context = _preview_context(
        summary=summary, data=data, raw_text=raw_text, dev_tools_enabled=dev_tools_enabled,
    )
    return templates.TemplateResponse(request, "config_import_preview.html", context)


@router.post("/config/import-export/apply")
async def config_import_apply_action(
    request: Request,
    config_json: str = Form(...),
    confirm_overwrite: str | None = Form(None),
    confirm_custom_code: str | None = Form(None),
    conn: sqlite3.Connection = Depends(get_db),
):
    try:
        data = json.loads(config_json)
    except json.JSONDecodeError:
        return _reject_to_landing("the carried-over config data was not valid JSON")

    dev_tools_enabled = _dev_tools_enabled(conn)
    try:
        # Re-validated fresh (not trusting whatever the preview page saw) —
        # dry_run=True makes no writes, so this is safe to run again right
        # before the real one below.
        preview_summary = import_config(
            conn, data, allow_custom_code=dev_tools_enabled, actor="ui.config.import",
            dry_run=True,
        )
    except ValueError as e:
        return _reject_to_landing(str(e))

    context = _preview_context(
        summary=preview_summary, data=data, raw_text=config_json, dev_tools_enabled=dev_tools_enabled,
    )

    errors = []
    if context["needs_overwrite_confirm"] and not confirm_overwrite:
        errors.append("Check the confirmation box to apply this import.")
    if context["custom_previews"] and not confirm_custom_code:
        errors.append(
            "Check the code-review confirmation box to import the CUSTOM adapter(s) listed above."
        )
    if errors:
        context["error"] = " ".join(errors)
        return templates.TemplateResponse(
            request, "config_import_preview.html", context, status_code=400
        )

    summary = import_config(
        conn, data, allow_custom_code=dev_tools_enabled, actor="ui.config.import", dry_run=False
    )
    return templates.TemplateResponse(request, "config_import_result.html", {"summary": summary})
