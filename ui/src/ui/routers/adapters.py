import dataclasses
import html
import json
import sqlite3
from typing import Any
from urllib.parse import urlencode

from adapters.aiprompt_adapter import AiPromptAdapter
from adapters.api_adapter import ApiAdapter, FieldMapping, preview_response
from adapters.custom_adapter import CustomAdapter
from adapters.actions_defaults import AI_PROMPT_DEFAULT
from adapters.categories import CATEGORIES, get_category
from adapters.config_transfer import build_adapter_export, import_adapter_export
from adapters.storage import (
    delete_adapter_instance,
    get_adapter_instance,
    get_setting,
    get_source_fields,
    list_adapter_instances,
    list_settings,
    set_adapter_instance,
    set_adapter_instance_enabled,
    set_source,
)
from adapters.policy import DEFAULT_POLICY_NAME, describe_policy, list_policies, resolve_policy
from adapters.timeutil import utc_now
from adapters.voice_replacements import SUGGESTED_VOICE_REPLACEMENTS
from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile
from markupsafe import Markup
from starlette.datastructures import FormData
from starlette.responses import RedirectResponse, Response

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

# Sentinel posted by the aiprompt type/subtype <select> (see adapter_form.html)
# when the operator picks "Other / custom" instead of a curated
# adapters.categories key — the actual value then comes from the paired
# aip_type_other/aip_subtype_other text input.
OTHER_KEY = "__other__"


def _categories_payload() -> list[dict]:
    """adapters.categories.CATEGORIES as plain dicts — Jinja's |tojson needs
    JSON-serializable data (dataclasses aren't), and the template also
    iterates this directly for the curated <select> options and the
    custom-adapter cheat-sheet panel."""
    return [
        {
            "key": c.key,
            "label_en": c.label_en,
            "label_es": c.label_es,
            "icon": c.icon,
            "subtypes": [
                {"key": s.key, "label_en": s.label_en, "label_es": s.label_es}
                for s in c.subtypes
            ],
        }
        for c in CATEGORIES
    ]


def _select_state(value: str, choices: tuple[str, ...]) -> tuple[str, str]:
    """A stored/posted string -> (select_value, other_text): the value
    itself when it's one of `choices`, else the OTHER_KEY sentinel plus the
    raw value as the free-text fallback (or ("", "") when blank)."""
    if not value:
        return "", ""
    if value in choices:
        return value, ""
    return OTHER_KEY, value


def _category_select_state(type_value: str, subtype_value: str) -> dict:
    """Resolves a stored (type, subtype) string pair into the curated
    <select> + "Other" free-text state adapter_form.html's aiprompt fields
    render, keeping the original resolved strings too (what
    _form_to_config actually stores) so the rest of the pipeline doesn't
    need to know this UI exists."""
    type_key, type_other = _select_state(type_value, tuple(c.key for c in CATEGORIES))
    category = get_category(type_key) if type_key not in ("", OTHER_KEY) else None
    subtype_choices = tuple(s.key for s in category.subtypes) if category else ()
    subtype_key, subtype_other = _select_state(subtype_value, subtype_choices)
    return {
        "type": type_value,
        "type_key": type_key,
        "type_other": type_other,
        "subtype": subtype_value,
        "subtype_key": subtype_key,
        "subtype_other": subtype_other,
    }


def _resolve_select_other(form: FormData, select_name: str, other_name: str) -> str:
    """The posted <select>+"Other" pair -> the single final string to
    store, mirroring _category_select_state's inverse."""
    value = (form.get(select_name) or "").strip()
    if value == OTHER_KEY:
        return (form.get(other_name) or "").strip()
    return value


def _build_test_adapter(source: str, adapter_type: str, config: dict, policy=None):
    adapter_class = _ADAPTER_CLASSES.get(adapter_type)
    if adapter_class is None:
        raise HTTPException(status_code=400, detail=f"unknown adapter_type {adapter_type!r}")
    return adapter_class(source=source, config=config, policy=policy)


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


def _json_to_html(
    value: Any, indent: int = 0, interactive_keys: bool = False, path_prefix: str = ""
) -> str:
    """Renders a parsed JSON value as indented, syntax-colored HTML (all
    text content escaped here — the result is only ever wrapped in Markup
    by its one caller, adapter_sample_action, never passed through
    unescaped from anywhere else). When `interactive_keys` is set, every
    key at every depth renders as a draggable/clickable span carrying
    `data-field-name` — the full dotted path from the sample item's root
    (e.g. dragging "nombre" nested inside "estacion" carries
    "estacion.nombre"), matching exactly what FieldMapping's dotted-path
    support (see adapters.api_adapter.resolve_dotted_path) resolves at
    fetch time. A list's own elements share their parent's path_prefix
    (no index appended), mirroring that same resolution's "a list along
    the path is its first element" rule — so what an operator drags is
    always a path that actually resolves."""
    pad = "  " * indent
    pad_close = "  " * (indent - 1) if indent > 0 else ""
    if isinstance(value, dict):
        if not value:
            return "{}"
        parts = []
        for key, val in value.items():
            escaped_key = html.escape(str(key))
            field_path = f"{path_prefix}.{key}" if path_prefix else str(key)
            if interactive_keys:
                escaped_path = html.escape(field_path)
                key_html = (
                    f'<span class="json-key draggable-field" draggable="true" '
                    f'data-field-name="{escaped_path}">"{escaped_key}"</span>'
                )
            else:
                key_html = f'<span class="json-key">"{escaped_key}"</span>'
            parts.append(
                f"{pad}{key_html}: {_json_to_html(val, indent + 1, interactive_keys, field_path)}"
            )
        return "{\n" + ",\n".join(parts) + "\n" + pad_close + "}"
    if isinstance(value, list):
        if not value:
            return "[]"
        parts = [
            f"{pad}{_json_to_html(v, indent + 1, interactive_keys, path_prefix)}" for v in value
        ]
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
            "title_template": config.get("title_template", ""),
            **_category_select_state(config.get("type", ""), config.get("subtype", "")),
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

    return {
        "url": config.get("url") or "",
        "method": config.get("method") or "GET",
        "response_format": config.get("response_format") or "json",
        "items_path": config.get("items_path") or "",
        "body": config.get("body") or "",
        "headers": [{"key": k, "value": v} for k, v in (config.get("headers") or {}).items()],
        "query_params": [{"key": k, "value": v} for k, v in (config.get("query_params") or {}).items()],
        "mapping": mapping,
        "date_field": config.get("date_field") or "",
        "date_format": config.get("date_format") or "",
        "source_timezone": config.get("source_timezone") or "",
    }


def _form_to_fields(adapter_type: str, form: FormData) -> dict:
    """Posted form -> the same flat field-value shape as _config_to_fields —
    used to re-render the form (with everything the user typed intact) on
    a validation error, without round-tripping through config parsing."""
    if adapter_type == "custom":
        return {"code": form.get("code") or ""}

    if adapter_type == "aiprompt":
        fields = {name: (form.get(f"aip_{name}") or "") for name in AIPROMPT_FIELDS}
        type_value = _resolve_select_other(form, "aip_type", "aip_type_other")
        subtype_value = _resolve_select_other(form, "aip_subtype", "aip_subtype_other")
        fields.update(_category_select_state(type_value, subtype_value))
        return fields

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
        "response_format": form.get("response_format") or "json",
        "items_path": form.get("items_path") or "",
        "body": form.get("body") or "",
        "headers": headers,
        "query_params": query_params,
        "mapping": mapping,
        "date_field": form.get("date_field") or "",
        "date_format": form.get("date_format") or "",
        "source_timezone": form.get("source_timezone") or "",
    }


def _form_to_config(adapter_type: str, form: FormData) -> dict:
    """Posted form -> the config dict to store/test — the inverse of
    _config_to_fields. Raises ValueError on bad input (missing URL, a
    non-integer number, a missing prompt); callers catch it
    and re-render via _form_to_fields so nothing the user typed is lost."""
    fields = _form_to_fields(adapter_type, form)
    if adapter_type == "custom":
        return {"code": fields["code"]}

    if adapter_type == "aiprompt":
        prompt = fields["prompt"].strip()
        if not prompt:
            raise ValueError("Prompt is required.")
        config = {"prompt": prompt}
        for key in ("title_template", "type", "subtype", "event_key_template"):
            value = fields[key].strip()
            if value:
                config[key] = value
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
        "response_format": fields["response_format"],
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
    return config


def _form_context(
    *,
    mode: str,
    source: str,
    display_name: str,
    site_url: str,
    adapter_type: str,
    enabled: bool,
    policy: str,
    ai_prompt: str,
    ai_prompt_default: str,
    ai_on_failure: str,
    use_ai: bool,
    voice_replacements: list[dict],
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
        "policy": policy,
        "ai_prompt": ai_prompt,
        # Shown (read-only) as the textarea's placeholder so an operator can
        # see what the summarizer will use when this box is left blank — the
        # global ACTIONS_AI_PROMPT override if set, else the built-in default.
        "ai_prompt_default": ai_prompt_default,
        # When checked, actions.ai stores the item's title as the summary if a
        # provider call fails, instead of letting the item hard-stop.
        "ai_on_failure": ai_on_failure,
        # Per-adapter AI on/off (actions.ai._resolve_use_ai) — lets an
        # adapter whose mapping already produces good contents skip
        # summarization entirely, independent of the global
        # ACTIONS_AI_ENABLED switch. Defaults to True (checked).
        "use_ai": use_ai,
        # Per-adapter TTS pronunciation map (key -> spoken expansion), applied
        # by the beacon to voice text only. Rows the operator has saved, as
        # [{"key":.., "value":..}]; empty for a new/unconfigured adapter.
        "voice_replacements": voice_replacements,
        # Read-only starting point the "Load suggested set" button drops in —
        # never applied on its own.
        "suggested_voice_replacements": SUGGESTED_VOICE_REPLACEMENTS,
        "mapped_fields": MAPPED_FIELDS,
        # adapters.categories.CATEGORIES as plain dicts, for the curated
        # type/subtype <select>s (aiprompt), the mapping quick-pick
        # (api), and the custom-code reference panel.
        "categories": _categories_payload(),
        "api_fields": api_fields,
        "custom_fields": custom_fields,
        "aiprompt_fields": aiprompt_fields,
        # PolicyRow list for the single Policy <select>.
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
            policy=DEFAULT_POLICY_NAME,
            ai_prompt="",
            ai_prompt_default=_default_prompt_placeholder(conn),
            ai_on_failure="continue_with_contents",
            use_ai=True,
            voice_replacements=[],
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
            policy=row["policy"] or DEFAULT_POLICY_NAME,
            ai_prompt=config.get("ai_prompt", ""),
            ai_prompt_default=_default_prompt_placeholder(conn),
            ai_on_failure=(config.get("ai_on_failure")
                           or ("use_title" if config.get("ai_fallback_to_title") else "continue_with_contents")),
            use_ai=bool(config.get("use_ai", True)),
            voice_replacements=[
                {"key": k, "value": v}
                for k, v in (config.get("voice_replacements") or {}).items()
            ],
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


@router.get("/config/adapters/{source}/export")
def adapter_export_action(source: str, conn: sqlite3.Connection = Depends(get_db)):
    try:
        export = build_adapter_export(conn, source, actor="ui.adapters.export")
    except LookupError:
        raise HTTPException(status_code=404, detail="adapter instance not found")
    body = json.dumps(export, indent=2) + "\n"
    filename = f"radiobeacon-adapter-{source}-{utc_now().strftime('%Y%m%dT%H%M%SZ')}.json"
    return Response(
        body,
        media_type="application/json",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


def _voice_replacement_rows(form: FormData) -> list[dict]:
    """Posted pronunciation-map rows as [{"key":.., "value":..}], order and
    blanks preserved — kept intact for a re-render; blank keys are dropped
    only at save (mirrors the headers/query-params kv-tables)."""
    return [
        {"key": k, "value": v}
        for k, v in zip(form.getlist("vrepl_key"), form.getlist("vrepl_value"))
    ]


async def _read_common_form(request: Request) -> tuple[FormData, dict]:
    form = await request.form()
    common = {
        "mode": "edit" if form.get("mode") == "edit" else "create",
        "source": (form.get("source") or "").strip(),
        "display_name": (form.get("display_name") or "").strip(),
        "site_url": (form.get("site_url") or "").strip(),
        "adapter_type": (form.get("adapter_type") or "api").strip(),
        "enabled": form.get("enabled") == "on",
        "policy": (form.get("policy") or "").strip() or DEFAULT_POLICY_NAME,
        "ai_prompt": (form.get("ai_prompt") or "").strip(),
        "ai_on_failure": (form.get("ai_on_failure") or "continue_with_contents").strip(),
        "use_ai": form.get("use_ai") == "on",
        "voice_replacements": _voice_replacement_rows(form),
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
        policy=common["policy"],
        ai_prompt=common["ai_prompt"],
        ai_prompt_default=_default_prompt_placeholder(conn),
        ai_on_failure=common["ai_on_failure"],
        use_ai=common["use_ai"],
        voice_replacements=common.get("voice_replacements", []),
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
        adapter = _build_test_adapter(
            common["source"], common["adapter_type"], config,
            policy=resolve_policy(conn, common["policy"]),
        )
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
        # itself, so each dict directly inside it is one item, and every
        # key at every depth is draggable as the dotted path that resolves
        # it (see _json_to_html's docstring and
        # adapters.api_adapter.resolve_dotted_path).
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

    # "On AI failure", read by actions.ai (_resolve_ai_on_failure): what to do
    # when there's no real provider summary — "use_title" or "abort". Omitted
    # from the blob when it's the default, same as ai_prompt; the legacy
    # boolean is dropped on any save.
    config.pop("ai_fallback_to_title", None)
    if common["ai_on_failure"] != "continue_with_contents":
        config["ai_on_failure"] = common["ai_on_failure"]

    # Per-adapter AI on/off, read by actions.ai (_resolve_use_ai). Omitted
    # from the blob when true (the default), same omit-the-default
    # convention as ai_prompt/ai_on_failure, so an adapter that never
    # touches this checkbox stays invisible in its config.
    if not common["use_ai"]:
        config["use_ai"] = False

    # Per-adapter voice pronunciation map, read by the beacon
    # (beacon.content.resolve_voice_replacements). Blank keys dropped; the
    # whole key is omitted from the blob when empty, same as ai_prompt, so an
    # adapter with no rows means "feature off".
    voice_replacements = {
        row["key"].strip(): row["value"]
        for row in common["voice_replacements"]
        if row["key"].strip()
    }
    if voice_replacements:
        config["voice_replacements"] = voice_replacements

    set_adapter_instance(
        conn,
        source,
        common["adapter_type"],
        config,
        enabled=common["enabled"],
        policy=common["policy"] or None,
    )
    if common["display_name"]:
        set_source(conn, source, common["display_name"], common["site_url"] or None)

    msg = f"adapter '{source}' saved"
    warning = _supersede_guard_rail(conn, common["adapter_type"], common["policy"], config)
    if warning:
        msg = f"{msg} — {warning}"
    return RedirectResponse(url=f"/config/adapters?{urlencode({'msg': msg})}", status_code=303)


def _supersede_guard_rail(conn, adapter_type: str, policy_name: str, config: dict) -> str | None:
    """Warn when a repeating (interval/cron) transmit Policy is assigned to
    an adapter whose items won't carry a stable event_key — so beacon's
    supersede can't engage and every fetch adds a separate item that airs
    on its own full schedule. api: needs a mapped event_key. aiprompt: its
    event_key defaults to the stable occurrence key, so it's fine. custom:
    can't tell statically — skip."""
    sched = resolve_policy(conn, policy_name).transmit
    if sched.kind not in ("interval", "cron"):
        return None
    if adapter_type == "api" and not (config.get("mapping") or {}).get("event_key"):
        return (
            "this Policy repeats transmissions but the adapter maps no event_key — "
            "a fresher item can't supersede an older one, so they'll overlap on air"
        )
    return None


@router.post("/config/adapters/{source}/delete")
def adapter_delete_action(source: str, conn: sqlite3.Connection = Depends(get_db)):
    deleted = delete_adapter_instance(conn, source)
    msg = f"adapter '{source}' deleted" if deleted else f"adapter '{source}' not found"
    return RedirectResponse(url=f"/config/adapters?{urlencode({'msg': msg})}", status_code=303)


@router.post("/config/adapters/{source}/toggle")
def adapter_toggle_action(source: str, conn: sqlite3.Connection = Depends(get_db)):
    row = get_adapter_instance(conn, source)
    if row is None:
        raise HTTPException(status_code=404, detail="adapter instance not found")
    enabled = not bool(row["enabled"])
    set_adapter_instance_enabled(conn, source, enabled)
    msg = f"adapter '{source}' {'enabled' if enabled else 'disabled'}"
    return RedirectResponse(url=f"/config/adapters?{urlencode({'msg': msg})}", status_code=303)


# --------------------------------- per-adapter import ---------------------------------
# Counterpart to ui.routers.config_transfer's whole-DB import flow, scoped to
# one adapter_instances record — see adapters.config_transfer.
# import_adapter_export's docstring for the shared validation path. Unlike the
# whole-DB flow, a brand-new/non-custom adapter applies immediately with no
# confirm page; a confirm step only appears when overwriting an existing
# source or importing CUSTOM (exec()'d) code.

_ADAPTER_CODE_PREVIEW_CHARS = 400


def _coerce_adapter_entry(data: dict) -> dict:
    """data["adapter"] straight from an uploaded/carried-over file — never
    trust its shape; a malformed file (string/list/missing) becomes an
    empty dict here, which import_adapter_export/its own preview then
    reports as a normal 'missing source' skip rather than a 500."""
    raw = data.get("adapter")
    return dict(raw) if isinstance(raw, dict) else {}


def _adapter_import_reject(message: str) -> RedirectResponse:
    return RedirectResponse(
        url=f"/config/adapters/import?{urlencode({'error': message})}", status_code=303
    )


def _adapter_code_preview(entry: dict) -> dict | None:
    if not isinstance(entry, dict) or entry.get("adapter_type") != "custom":
        return None
    config = entry.get("config")
    code = config.get("code") if isinstance(config, dict) else None
    code = code if isinstance(code, str) else ""
    return {
        "code_preview": code[:_ADAPTER_CODE_PREVIEW_CHARS],
        "truncated": len(code) > _ADAPTER_CODE_PREVIEW_CHARS,
    }


def _adapter_import_preview_context(
    *, result, entry: dict, raw_text: str, override_source: str, error: str | None,
    dev_tools_enabled: bool,
) -> dict:
    return {
        "result": result,
        "raw_text": raw_text,
        "entry": entry,
        "code_preview": _adapter_code_preview(entry) if dev_tools_enabled else None,
        "skipped_custom": entry.get("adapter_type") == "custom" and not dev_tools_enabled,
        "needs_overwrite_confirm": result.action == "updated",
        "override_source": override_source,
        "error": error,
    }


@router.get("/config/adapters/import")
def adapter_import_page(request: Request):
    return templates.TemplateResponse(request, "adapter_import.html", {})


@router.post("/config/adapters/import/preview")
async def adapter_import_preview_action(
    request: Request,
    upload: UploadFile = File(...),
    conn: sqlite3.Connection = Depends(get_db),
):
    raw_bytes = await upload.read()
    try:
        raw_text = raw_bytes.decode("utf-8")
    except UnicodeDecodeError:
        return _adapter_import_reject("the uploaded file is not valid UTF-8 text")
    try:
        data = json.loads(raw_text)
    except json.JSONDecodeError as e:
        return _adapter_import_reject(f"the uploaded file is not valid JSON ({e})")
    if not isinstance(data, dict):
        return _adapter_import_reject("the uploaded file's top level must be a JSON object")

    dev_tools_enabled = _dev_tools_enabled(conn)
    try:
        result = import_adapter_export(
            conn, data, allow_custom_code=dev_tools_enabled, actor="ui.adapters.import",
            dry_run=True,
        )
    except ValueError as e:
        return _adapter_import_reject(str(e))

    entry = _coerce_adapter_entry(data)
    context = _adapter_import_preview_context(
        result=result, entry=entry, raw_text=raw_text,
        override_source=entry.get("source", ""), error=None,
        dev_tools_enabled=dev_tools_enabled,
    )

    # Nothing risky (brand-new source, not CUSTOM code) and nothing wrong
    # with the record — apply straight away, same as saving the form by hand.
    if result.action == "created" and context["code_preview"] is None:
        import_adapter_export(
            conn, data, allow_custom_code=dev_tools_enabled, actor="ui.adapters.import",
        )
        msg = f"adapter '{result.key}' imported"
        return RedirectResponse(url=f"/config/adapters?{urlencode({'msg': msg})}", status_code=303)

    return templates.TemplateResponse(request, "adapter_import_preview.html", context)


@router.post("/config/adapters/import/apply")
async def adapter_import_apply_action(
    request: Request,
    config_json: str = Form(...),
    override_source: str | None = Form(None),
    confirm_overwrite: str | None = Form(None),
    confirm_custom_code: str | None = Form(None),
    conn: sqlite3.Connection = Depends(get_db),
):
    try:
        data = json.loads(config_json)
    except json.JSONDecodeError:
        return _adapter_import_reject("the carried-over adapter data was not valid JSON")

    dev_tools_enabled = _dev_tools_enabled(conn)
    override = (override_source or "").strip() or None
    try:
        # Re-validated fresh (never trusting the confirm page) — dry_run=True
        # makes no writes, safe to run again right before the real apply.
        result = import_adapter_export(
            conn, data, allow_custom_code=dev_tools_enabled, actor="ui.adapters.import",
            override_source=override, dry_run=True,
        )
    except ValueError as e:
        return _adapter_import_reject(str(e))

    entry = _coerce_adapter_entry(data)
    if override:
        entry["source"] = override
    context = _adapter_import_preview_context(
        result=result, entry=entry, raw_text=config_json,
        override_source=override or entry.get("source", ""), error=None,
        dev_tools_enabled=dev_tools_enabled,
    )

    if result.action == "skipped":
        # Nothing to confirm — the record itself is invalid (bad
        # adapter_type/config) or CUSTOM code is refused on this install;
        # show why instead of silently "succeeding".
        return templates.TemplateResponse(
            request, "adapter_import_preview.html", context, status_code=400
        )

    errors = []
    if context["needs_overwrite_confirm"] and not confirm_overwrite:
        errors.append("Check the confirmation box to overwrite this existing adapter.")
    if context["code_preview"] is not None and not confirm_custom_code:
        errors.append("Check the code-review confirmation box to import this CUSTOM adapter.")
    if errors:
        context["error"] = " ".join(errors)
        return templates.TemplateResponse(
            request, "adapter_import_preview.html", context, status_code=400
        )

    result = import_adapter_export(
        conn, data, allow_custom_code=dev_tools_enabled, actor="ui.adapters.import",
        override_source=override, dry_run=False,
    )
    msg = f"adapter '{result.key}' imported"
    return RedirectResponse(url=f"/config/adapters?{urlencode({'msg': msg})}", status_code=303)
