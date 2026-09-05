import dataclasses
import html
import json
import sqlite3
from typing import Any
from urllib.parse import urlencode

from adapters import cron
from adapters.aiprompt_adapter import AiPromptAdapter
from adapters.api_adapter import ApiAdapter, FieldMapping, preview_response
from adapters.custom_adapter import CustomAdapter
from adapters.actions_defaults import AI_PROMPT_DEFAULT
from adapters.storage import (
    delete_adapter_instance,
    get_adapter_instance,
    get_setting,
    get_source_fields,
    list_adapter_instances,
    list_settings,
    set_adapter_instance,
    set_source,
)
from adapters.transmit_policy import DEFAULT_POLICY_NAME, list_policies
from fastapi import APIRouter, Depends, HTTPException, Request
from markupsafe import Markup
from starlette.datastructures import FormData
from starlette.responses import RedirectResponse

from .. import queries
from ..config_catalog import specs_for_group
from ..current_user import require_role
from ..db import get_db
from ..templating import templates

router = APIRouter(dependencies=[Depends(require_role("admin"))])

_ADAPTER_CLASSES = {"api": ApiAdapter, "custom": CustomAdapter, "aiprompt": AiPromptAdapter}


def _dev_tools_enabled(conn: sqlite3.Connection) -> bool:
    return get_setting("UI_DEV_TOOLS_ENABLED", "true", conn=conn).lower() not in (
        "false",
        "0",
        "",
    )


def _guard_custom_adapter(adapter_type: str, conn: sqlite3.Connection) -> None:
    """A CUSTOM adapter runs operator-authored Python via exec() on every
    poll — the same power the /dev SQL runner has, so it sits behind the same
    switch. With UI_DEV_TOOLS_ENABLED off (the hardened profile), CUSTOM
    adapters can't be created, edited, or test-run through the web form."""
    if adapter_type == "custom" and not _dev_tools_enabled(conn):
        raise HTTPException(
            status_code=403,
            detail="custom adapters are disabled (UI_DEV_TOOLS_ENABLED is off)",
        )

# The aiprompt-type fields, flat, in template-input-name order. Mirrors
# MAPPED_FIELDS' role for the API type — see _config_to_fields / _form_to_fields.
AIPROMPT_FIELDS = (
    "prompt",
    "cron",
    "transmit_policy",
    "title_template",
    "type",
    "subtype",
    "event_key_template",
)

# The 7 contract fields an API-type adapter can map — fixed and known, so
# the UI renders one static table row per name rather than a dynamic
# JS-managed list (unlike headers/query_params, whose key set is
# unbounded). See adapters.api_adapter.ApiAdapterConfig/FieldMapping.
MAPPED_FIELDS = ("id", "title", "contents", "url", "event_key", "type", "subtype")


def _build_test_adapter(source: str, adapter_type: str, config: dict):
    adapter_class = _ADAPTER_CLASSES.get(adapter_type)
    if adapter_class is None:
        raise HTTPException(status_code=400, detail=f"unknown adapter_type {adapter_type!r}")
    return adapter_class(source=source, config=config)


def _item_to_dict(item) -> dict:
    d = dataclasses.asdict(item)
    if d.get("source_date_time") is not None:
        d["source_date_time"] = str(d["source_date_time"])
    # `raw` (the unmapped source item — see AdapterItem) is shown
    # separately as syntax-highlighted JSON, not a "Test fetch" table
    # column: it's the one field that's a nested object rather than a
    # short scalar, and would blow out every row's height otherwise.
    d["raw_html"] = Markup(_json_to_html(d.pop("raw", None)))
    return d


def _json_to_html(value: Any, indent: int = 0, interactive_keys: bool = False) -> str:
    """Renders a parsed JSON value as indented, syntax-colored HTML (all
    text content escaped here — the result is only ever wrapped in Markup
    by its one caller, adapter_sample_action, never passed through
    unescaped from anywhere else). When `interactive_keys` is set, each
    key at *this* level renders as a draggable/clickable span carrying
    `data-field-name` — used only for the keys of each sample item itself
    (the list adapter_sample_action passes in), since FieldMapping.field
    is a flat `item.get(name)` with no dotted-path support into nested
    structures yet. Anything nested inside a value — even another list of
    dicts — renders as plain, non-interactive JSON, so the UI never
    offers a drag/click target that would silently fail to resolve."""
    pad = "  " * indent
    pad_close = "  " * (indent - 1) if indent > 0 else ""
    if isinstance(value, dict):
        if not value:
            return "{}"
        parts = []
        for key, val in value.items():
            escaped_key = html.escape(str(key))
            if interactive_keys:
                key_html = (
                    f'<span class="json-key draggable-field" draggable="true" '
                    f'data-field-name="{escaped_key}">"{escaped_key}"</span>'
                )
            else:
                key_html = f'<span class="json-key">"{escaped_key}"</span>'
            parts.append(f"{pad}{key_html}: {_json_to_html(val, indent + 1, False)}")
        return "{\n" + ",\n".join(parts) + "\n" + pad_close + "}"
    if isinstance(value, list):
        if not value:
            return "[]"
        parts = [f"{pad}{_json_to_html(v, indent + 1, interactive_keys)}" for v in value]
        return "[\n" + ",\n".join(parts) + "\n" + pad_close + "]"
    if isinstance(value, str):
        return f'<span class="json-string">"{html.escape(value)}"</span>'
    if isinstance(value, bool):
        return f'<span class="json-bool">{"true" if value else "false"}</span>'
    if value is None:
        return '<span class="json-null">null</span>'
    return f'<span class="json-number">{html.escape(str(value))}</span>'


def _config_to_fields(adapter_type: str, config: dict) -> dict:
    """DB config dict -> the flat field-value shape the template's inputs
    are named after — used to prefill the edit form. Mirrors
    _form_to_fields (the same shape, built from a posted form instead)."""
    if adapter_type == "custom":
        return {"code": config.get("code", "")}

    if adapter_type == "aiprompt":
        return {
            "prompt": config.get("prompt", ""),
            "cron": config.get("cron", ""),
            "transmit_policy": config.get("transmit_policy", "") or DEFAULT_POLICY_NAME,
            "title_template": config.get("title_template", ""),
            "type": config.get("type", ""),
            "subtype": config.get("subtype", ""),
            "event_key_template": config.get("event_key_template", ""),
        }

    mapping_raw = config.get("mapping") or {}
    mapping = {}
    for name in MAPPED_FIELDS:
        # FieldMapping.from_dict migrates the old {field, value, template}
        # shape transparently — reuse it here too, so an already-saved
        # instance (from before the single-template redesign) still
        # prefills correctly instead of showing blank rows.
        fm = FieldMapping.from_dict(mapping_raw.get(name))
        mapping[name] = {
            "template": fm.template or "",
            "date_format": fm.field_date_format or "",
        }

    # Back-compat: an unmigrated config may still hold the removed threshold
    # rule — surface its non-escalated branch as the plain policy selection.
    legacy_rule = config.get("transmit_policy_rule") or config.get("dispatch_policy_rule") or {}
    return {
        "url": config.get("url") or "",
        "method": config.get("method") or "GET",
        "items_path": config.get("items_path") or "",
        "body": config.get("body") or "",
        "headers": [{"key": k, "value": v} for k, v in (config.get("headers") or {}).items()],
        "query_params": [{"key": k, "value": v} for k, v in (config.get("query_params") or {}).items()],
        "mapping": mapping,
        "date_field": config.get("date_field") or "",
        "date_format": config.get("date_format") or "",
        "source_timezone": config.get("source_timezone") or "",
        "transmit_policy": (
            config.get("transmit_policy")
            or legacy_rule.get("if_false")
            or DEFAULT_POLICY_NAME
        ),
    }


def _form_to_fields(adapter_type: str, form: FormData) -> dict:
    """Posted form -> the same flat field-value shape as _config_to_fields —
    used to re-render the form (with everything the user typed intact) on
    a validation error, without round-tripping through config parsing."""
    if adapter_type == "custom":
        return {"code": form.get("code") or ""}

    if adapter_type == "aiprompt":
        return {name: (form.get(f"aip_{name}") or "") for name in AIPROMPT_FIELDS}

    mapping = {
        name: {
            "template": form.get(f"map_{name}_template") or "",
            "date_format": form.get(f"map_{name}_date_format") or "",
        }
        for name in MAPPED_FIELDS
    }
    headers = [
        {"key": k, "value": v}
        for k, v in zip(form.getlist("header_key"), form.getlist("header_value"))
    ]
    query_params = [
        {"key": k, "value": v}
        for k, v in zip(form.getlist("param_key"), form.getlist("param_value"))
    ]
    return {
        "url": form.get("url") or "",
        "method": form.get("method") or "GET",
        "items_path": form.get("items_path") or "",
        "body": form.get("body") or "",
        "headers": headers,
        "query_params": query_params,
        "mapping": mapping,
        "date_field": form.get("date_field") or "",
        "date_format": form.get("date_format") or "",
        "source_timezone": form.get("source_timezone") or "",
        "transmit_policy": form.get("transmit_policy") or DEFAULT_POLICY_NAME,
    }


def _form_to_config(adapter_type: str, form: FormData) -> dict:
    """Posted form -> the config dict to store/test — the inverse of
    _config_to_fields. Raises ValueError on bad input (missing URL, a
    non-integer max_tokens, an invalid aiprompt cron); callers catch it
    and re-render via _form_to_fields so nothing the user typed is lost."""
    fields = _form_to_fields(adapter_type, form)
    if adapter_type == "custom":
        return {"code": fields["code"]}

    if adapter_type == "aiprompt":
        prompt = fields["prompt"].strip()
        if not prompt:
            raise ValueError("Prompt is required.")
        cron_expr = fields["cron"].strip()
        if not cron_expr:
            raise ValueError("A cron expression is required.")
        if not cron.is_valid_cron(cron_expr):
            raise ValueError(f"Cron expression {cron_expr!r} is not valid.")
        config = {"prompt": prompt, "cron": cron_expr}
        for key in ("title_template", "type", "subtype", "event_key_template"):
            value = fields[key].strip()
            if value:
                config[key] = value
        policy = fields["transmit_policy"].strip()
        if policy and policy != DEFAULT_POLICY_NAME:
            config["transmit_policy"] = policy
        return config

    if not fields["url"].strip():
        raise ValueError("URL is required.")

    mapping = {}
    for name in MAPPED_FIELDS:
        row = fields["mapping"][name]
        template = row["template"].strip()
        if not template:
            continue
        entry: dict = {"template": template}
        date_format = row["date_format"].strip()
        if date_format:
            entry["field_date_format"] = date_format
        mapping[name] = entry

    config: dict = {
        "url": fields["url"].strip(),
        "method": fields["method"],
        "headers": {row["key"]: row["value"] for row in fields["headers"] if row["key"].strip()},
        "query_params": {
            row["key"]: row["value"] for row in fields["query_params"] if row["key"].strip()
        },
        "items_path": fields["items_path"].strip(),
        "mapping": mapping,
    }
    if fields["body"]:
        config["body"] = fields["body"]
    if fields["date_field"].strip():
        config["date_field"] = fields["date_field"].strip()
        if fields["date_format"].strip():
            config["date_format"] = fields["date_format"].strip()
        if fields["source_timezone"].strip():
            config["source_timezone"] = fields["source_timezone"].strip()
    # Omit when it's just the default, to keep the stored blob tidy — a NULL
    # items.transmit_policy resolves to DEFAULT_POLICY_NAME anyway.
    if fields["transmit_policy"] and fields["transmit_policy"] != DEFAULT_POLICY_NAME:
        config["transmit_policy"] = fields["transmit_policy"]
    return config


def _form_context(
    *,
    mode: str,
    source: str,
    display_name: str,
    site_url: str,
    adapter_type: str,
    enabled: bool,
    interval_seconds: str,
    ai_prompt: str,
    ai_prompt_default: str,
    ai_fallback_to_title: bool,
    api_fields: dict,
    custom_fields: dict,
    aiprompt_fields: dict,
    policies: list,
    test_result: dict | None,
    error: str | None,
) -> dict:
    return {
        "mode": mode,
        "source": source,
        "display_name": display_name,
        "site_url": site_url,
        "adapter_type": adapter_type,
        "enabled": enabled,
        "interval_seconds": interval_seconds,
        "ai_prompt": ai_prompt,
        # Shown (read-only) as the textarea's placeholder so an operator can
        # see what the summarizer will use when this box is left blank — the
        # global ACTIONS_AI_PROMPT override if set, else the built-in default.
        "ai_prompt_default": ai_prompt_default,
        # When checked, actions.ai stores the item's title as the summary if a
        # provider call fails, instead of letting the item hard-stop.
        "ai_fallback_to_title": ai_fallback_to_title,
        "mapped_fields": MAPPED_FIELDS,
        "api_fields": api_fields,
        "custom_fields": custom_fields,
        "aiprompt_fields": aiprompt_fields,
        # (name, repeat_times, interval_seconds, description) rows for the
        # transmit-policy <select>s in the api / aiprompt fieldsets.
        "policies": policies,
        "default_policy_name": DEFAULT_POLICY_NAME,
        "test_result": test_result,
        "sample_response": None,
        "error": error,
    }


@router.get("/adapters", include_in_schema=False)
def adapters_legacy_redirect():
    """The Adapters page moved under /config — keep old bookmarks working."""
    return RedirectResponse(url="/config/adapters", status_code=307)


def _general_settings_fields(conn: sqlite3.Connection) -> list[dict]:
    """The "Adapters — General" catalog group (currently just the default
    poll interval), shown inline at the top of this page instead of on its
    own tab — this page is the single Adapters section, covering both that
    shared setting and every per-source instance below."""
    overrides = {row["key"]: row for row in list_settings(conn)}
    fields = []
    for spec in specs_for_group("adapters-general"):
        row = overrides.get(spec.key)
        value = get_setting(spec.key, spec.default, conn=conn, env_fallback=spec.env_fallback) or ""
        fields.append({"spec": spec, "value": value, "overridden": row is not None})
    return fields


@router.get("/config/adapters")
def adapters_list_page(request: Request, conn: sqlite3.Connection = Depends(get_db)):
    instances = list_adapter_instances(conn)
    last_events = queries.last_adapter_fetch_events(conn)
    rows = []
    for row in instances:
        event = last_events.get(row["source"])
        last_fetch = None
        if event is not None:
            details = json.loads(event["details"]) if event["details"] else {}
            last_fetch = {"recorded_at": event["recorded_at"], "ok": details.get("ok")}
        rows.append({**row, "last_fetch": last_fetch})
    return templates.TemplateResponse(
        request,
        "adapters_list.html",
        {"rows": rows, "general_fields": _general_settings_fields(conn)},
    )


def _default_prompt_placeholder(conn: sqlite3.Connection | None) -> str:
    """What the AI summarizer falls back to when an adapter's "AI prompt
    override" box is left blank — the global ACTIONS_AI_PROMPT setting if
    one is stored, otherwise the built-in default. Shown read-only as the
    textarea placeholder. conn=None (a re-render path with no DB handle)
    just uses the built-in default."""
    if conn is None:
        return AI_PROMPT_DEFAULT
    return get_setting("ACTIONS_AI_PROMPT", AI_PROMPT_DEFAULT, conn=conn)


@router.get("/config/adapters/new")
def adapter_new_page(request: Request, conn: sqlite3.Connection = Depends(get_db)):
    return templates.TemplateResponse(
        request,
        "adapter_form.html",
        _form_context(
            mode="create",
            source="",
            display_name="",
            site_url="",
            adapter_type="api",
            enabled=True,
            interval_seconds="",
            ai_prompt="",
            ai_prompt_default=_default_prompt_placeholder(conn),
            ai_fallback_to_title=False,
            api_fields=_config_to_fields("api", {}),
            custom_fields=_config_to_fields("custom", {}),
            aiprompt_fields=_config_to_fields("aiprompt", {}),
            policies=list_policies(conn),
            test_result=None,
            error=None,
        ),
    )


@router.get("/config/adapters/{source}/edit")
def adapter_edit_page(request: Request, source: str, conn: sqlite3.Connection = Depends(get_db)):
    row = get_adapter_instance(conn, source)
    if row is None:
        raise HTTPException(status_code=404, detail="adapter instance not found")
    source_fields = get_source_fields(conn, source)
    config = json.loads(row["config"])
    return templates.TemplateResponse(
        request,
        "adapter_form.html",
        _form_context(
            mode="edit",
            source=row["source"],
            display_name=source_fields["source_name"],
            site_url=source_fields["source_url"],
            adapter_type=row["adapter_type"],
            enabled=bool(row["enabled"]),
            interval_seconds=str(row["interval_seconds"]) if row["interval_seconds"] else "",
            ai_prompt=config.get("ai_prompt", ""),
            ai_prompt_default=_default_prompt_placeholder(conn),
            ai_fallback_to_title=bool(config.get("ai_fallback_to_title", False)),
            api_fields=_config_to_fields("api", config if row["adapter_type"] == "api" else {}),
            custom_fields=_config_to_fields(
                "custom", config if row["adapter_type"] == "custom" else {}
            ),
            aiprompt_fields=_config_to_fields(
                "aiprompt", config if row["adapter_type"] == "aiprompt" else {}
            ),
            policies=list_policies(conn),
            test_result=None,
            error=None,
        ),
    )


async def _read_common_form(request: Request) -> tuple[FormData, dict]:
    form = await request.form()
    common = {
        "mode": "edit" if form.get("mode") == "edit" else "create",
        "source": (form.get("source") or "").strip(),
        "display_name": (form.get("display_name") or "").strip(),
        "site_url": (form.get("site_url") or "").strip(),
        "adapter_type": (form.get("adapter_type") or "api").strip(),
        "enabled": form.get("enabled") == "on",
        "interval_seconds": (form.get("interval_seconds") or "").strip(),
        "ai_prompt": (form.get("ai_prompt") or "").strip(),
        "ai_fallback_to_title": form.get("ai_fallback_to_title") == "on",
    }
    return form, common


def _error_context(
    common: dict, form: FormData, message: str | None, *, conn: sqlite3.Connection | None = None
) -> dict:
    return _form_context(
        mode=common["mode"],
        source=common["source"],
        display_name=common["display_name"],
        site_url=common["site_url"],
        adapter_type=common["adapter_type"],
        enabled=common["enabled"],
        interval_seconds=common["interval_seconds"],
        ai_prompt=common["ai_prompt"],
        ai_prompt_default=_default_prompt_placeholder(conn),
        ai_fallback_to_title=common["ai_fallback_to_title"],
        api_fields=_form_to_fields("api", form),
        custom_fields=_form_to_fields("custom", form),
        aiprompt_fields=_form_to_fields("aiprompt", form),
        policies=list_policies(conn) if conn is not None else [],
        test_result=None,
        error=message,
    )


@router.post("/config/adapters/test")
async def adapter_test_action(request: Request, conn: sqlite3.Connection = Depends(get_db)):
    form, common = await _read_common_form(request)

    if not common["source"]:
        context = _error_context(common, form, "Source is required to run a test fetch.", conn=conn)
        return templates.TemplateResponse(request, "adapter_form.html", context)
    _guard_custom_adapter(common["adapter_type"], conn)

    try:
        config = _form_to_config(common["adapter_type"], form)
    except ValueError as e:
        context = _error_context(common, form, str(e), conn=conn)
        return templates.TemplateResponse(request, "adapter_form.html", context)

    context = _error_context(common, form, None, conn=conn)
    try:
        adapter = _build_test_adapter(common["source"], common["adapter_type"], config)
        reading = adapter.fetch()
    except HTTPException:
        raise
    except Exception as e:
        context = _error_context(common, form, f"Test fetch failed: {e}", conn=conn)
        return templates.TemplateResponse(request, "adapter_form.html", context)
    context["test_result"] = {
        "ok": reading.ok,
        "error": reading.error,
        # Not "items" — dict has a builtin .items() method, and Jinja's
        # dot-access tries getattr first, so `test_result.items` in the
        # template would silently return that bound method instead of
        # doing a dict lookup.
        "sample_items": [_item_to_dict(item) for item in reading.data[:5]],
        "total": len(reading.data) if reading.ok else 0,
    }
    return templates.TemplateResponse(request, "adapter_form.html", context)


@router.post("/config/adapters/sample")
async def adapter_sample_action(request: Request, conn: sqlite3.Connection = Depends(get_db)):
    """Calls the endpoint with whatever connection fields (url/method/
    headers/query_params/body/items_path) are currently in the form and
    shows the raw, unmapped response — so an operator can see a source's
    actual field names before configuring (or while configuring) the
    field-mapping table below, instead of guessing key names blind. Only
    meaningful for API-type instances; CUSTOM type has no connection
    fields to preview (its code decides everything itself)."""
    form, common = await _read_common_form(request)

    if common["adapter_type"] != "api":
        context = _error_context(
            common, form, "Sample fetch is only available for API-type adapters.", conn=conn
        )
        return templates.TemplateResponse(request, "adapter_form.html", context)

    try:
        config = _form_to_config("api", form)
        preview = preview_response(config)
    except Exception as e:
        context = _error_context(common, form, f"Sample fetch failed: {e}", conn=conn)
        return templates.TemplateResponse(request, "adapter_form.html", context)

    context = _error_context(common, form, None, conn=conn)
    items = preview["items"]
    context["sample_response"] = {
        # interactive_keys=True: `items` is the list of sample items
        # itself, so each dict directly inside it is one item — its own
        # keys are exactly what FieldMapping.field can reference (see
        # _json_to_html's docstring for why nested keys stay plain text).
        "highlighted_html": Markup(_json_to_html(items, interactive_keys=True)),
        "field_names": sorted({key for item in items if isinstance(item, dict) for key in item}),
        "count": len(items),
        "total_count": preview["total_count"],
        "is_list": preview["is_list"],
        "items_path_resolved": preview["items_path_resolved"],
        # Every array-of-dicts path found in the response, so "is this a
        # list, and is it the right one?" is answered directly instead of
        # the operator guessing a dotted items_path blind — see
        # adapters.api_adapter.preview_response.
        "candidates": preview["candidates"],
    }
    return templates.TemplateResponse(request, "adapter_form.html", context)


# Registered before the dynamic /config/adapters/{existing_source} route
# below — FastAPI/Starlette matches path routes in registration order, so a
# static path (a literal "test" here would otherwise be captured as
# existing_source) must come first, same reasoning as /config/adapters/new
# above needing to be a distinct GET path from /config/adapters/{source}/edit.
@router.post("/config/adapters")
@router.post("/config/adapters/{existing_source}")
async def adapter_save_action(
    request: Request,
    existing_source: str | None = None,
    conn: sqlite3.Connection = Depends(get_db),
):
    form, common = await _read_common_form(request)
    common["mode"] = "edit" if existing_source else "create"
    source = existing_source or common["source"]
    common["source"] = source

    if not source:
        context = _error_context(common, form, "Source is required.", conn=conn)
        return templates.TemplateResponse(request, "adapter_form.html", context, status_code=400)
    if common["adapter_type"] not in _ADAPTER_CLASSES:
        context = _error_context(
            common, form, f"Unknown adapter type {common['adapter_type']!r}.", conn=conn
        )
        return templates.TemplateResponse(request, "adapter_form.html", context, status_code=400)
    _guard_custom_adapter(common["adapter_type"], conn)

    try:
        config = _form_to_config(common["adapter_type"], form)
    except ValueError as e:
        context = _error_context(common, form, str(e), conn=conn)
        return templates.TemplateResponse(request, "adapter_form.html", context, status_code=400)

    # Optional per-adapter summarization prompt override, read by actions.ai
    # (see _resolve_prompt_template). Stored alongside the fetch config; left
    # out of the blob entirely when blank so an unused override never shows.
    if common["ai_prompt"]:
        config["ai_prompt"] = common["ai_prompt"]

    # Optional per-adapter opt-in, also read by actions.ai: store the item's
    # title as the summary (instead of the full extracted contents) whenever AI
    # can't run — on a failed provider call, keeping the pipeline flowing rather
    # than letting the item hard-stop, and on the AI-disabled skip. Omitted when
    # off, same as ai_prompt — a plain CUSTOM config stays just {"code": ...}.
    if common["ai_fallback_to_title"]:
        config["ai_fallback_to_title"] = True

    interval_seconds = int(common["interval_seconds"]) if common["interval_seconds"] else None

    set_adapter_instance(
        conn,
        source,
        common["adapter_type"],
        config,
        enabled=common["enabled"],
        interval_seconds=interval_seconds,
    )
    if common["display_name"]:
        set_source(conn, source, common["display_name"], common["site_url"] or None)

    msg = urlencode({"msg": f"adapter '{source}' saved"})
    return RedirectResponse(url=f"/config/adapters?{msg}", status_code=303)


@router.post("/config/adapters/{source}/delete")
def adapter_delete_action(source: str, conn: sqlite3.Connection = Depends(get_db)):
    deleted = delete_adapter_instance(conn, source)
    msg = f"adapter '{source}' deleted" if deleted else f"adapter '{source}' not found"
    return RedirectResponse(url=f"/config/adapters?{urlencode({'msg': msg})}", status_code=303)
