import dataclasses
import html
import json
import sqlite3
from typing import Any
from urllib.parse import urlencode

from adapters.api_adapter import ApiAdapter, FieldMapping, preview_response
from adapters.custom_adapter import CustomAdapter
from adapters.storage import (
    delete_adapter_instance,
    get_adapter_instance,
    get_source_fields,
    list_adapter_instances,
    set_adapter_instance,
    set_source,
)
from fastapi import APIRouter, Depends, HTTPException, Request
from markupsafe import Markup
from starlette.datastructures import FormData
from starlette.responses import RedirectResponse

from .. import queries
from ..db import get_db
from ..templating import templates

router = APIRouter()

_ADAPTER_CLASSES = {"api": ApiAdapter, "custom": CustomAdapter}

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

    # Back-compat: a config saved before the rename may still hold the old key.
    rule = config.get("transmit_policy_rule") or config.get("dispatch_policy_rule") or {}
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
        "transmit_enabled": bool(
            config.get("transmit_policy_rule") or config.get("dispatch_policy_rule")
        ),
        "transmit_field": rule.get("field", ""),
        "transmit_operator": rule.get("operator", ">="),
        "transmit_threshold": str(rule["threshold"]) if "threshold" in rule else "",
        "transmit_if_true": rule.get("if_true", "urgent"),
        "transmit_if_false": rule.get("if_false", "informational"),
    }


def _form_to_fields(adapter_type: str, form: FormData) -> dict:
    """Posted form -> the same flat field-value shape as _config_to_fields —
    used to re-render the form (with everything the user typed intact) on
    a validation error, without round-tripping through config parsing."""
    if adapter_type == "custom":
        return {"code": form.get("code") or ""}

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
        "transmit_enabled": form.get("transmit_enabled") == "on",
        "transmit_field": form.get("transmit_field") or "",
        "transmit_operator": form.get("transmit_operator") or ">=",
        "transmit_threshold": form.get("transmit_threshold") or "",
        "transmit_if_true": form.get("transmit_if_true") or "urgent",
        "transmit_if_false": form.get("transmit_if_false") or "informational",
    }


def _form_to_config(adapter_type: str, form: FormData) -> dict:
    """Posted form -> the config dict to store/test — the inverse of
    _config_to_fields. Raises ValueError on bad numeric input (the
    dispatch threshold); callers catch it and re-render via
    _form_to_fields so nothing the user typed is lost."""
    fields = _form_to_fields(adapter_type, form)
    if adapter_type == "custom":
        return {"code": fields["code"]}

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
    if fields["transmit_enabled"]:
        config["transmit_policy_rule"] = {
            "field": fields["transmit_field"].strip(),
            "operator": fields["transmit_operator"],
            "threshold": float(fields["transmit_threshold"] or "0"),
            "if_true": fields["transmit_if_true"].strip() or "urgent",
            "if_false": fields["transmit_if_false"].strip() or "informational",
        }
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
    api_fields: dict,
    custom_fields: dict,
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
        "mapped_fields": MAPPED_FIELDS,
        "api_fields": api_fields,
        "custom_fields": custom_fields,
        "test_result": test_result,
        "sample_response": None,
        "error": error,
    }


@router.get("/adapters")
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
    return templates.TemplateResponse(request, "adapters_list.html", {"rows": rows})


@router.get("/adapters/new")
def adapter_new_page(request: Request):
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
            api_fields=_config_to_fields("api", {}),
            custom_fields=_config_to_fields("custom", {}),
            test_result=None,
            error=None,
        ),
    )


@router.get("/adapters/{source}/edit")
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
            api_fields=_config_to_fields("api", config if row["adapter_type"] == "api" else {}),
            custom_fields=_config_to_fields(
                "custom", config if row["adapter_type"] == "custom" else {}
            ),
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
    }
    return form, common


def _error_context(common: dict, form: FormData, message: str | None) -> dict:
    return _form_context(
        mode=common["mode"],
        source=common["source"],
        display_name=common["display_name"],
        site_url=common["site_url"],
        adapter_type=common["adapter_type"],
        enabled=common["enabled"],
        interval_seconds=common["interval_seconds"],
        api_fields=_form_to_fields("api", form),
        custom_fields=_form_to_fields("custom", form),
        test_result=None,
        error=message,
    )


@router.post("/adapters/test")
async def adapter_test_action(request: Request):
    form, common = await _read_common_form(request)

    if not common["source"]:
        context = _error_context(common, form, "Source is required to run a test fetch.")
        return templates.TemplateResponse(request, "adapter_form.html", context)

    try:
        config = _form_to_config(common["adapter_type"], form)
    except ValueError as e:
        context = _error_context(common, form, str(e))
        return templates.TemplateResponse(request, "adapter_form.html", context)

    context = _error_context(common, form, None)
    adapter = _build_test_adapter(common["source"], common["adapter_type"], config)
    reading = adapter.fetch()
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


@router.post("/adapters/sample")
async def adapter_sample_action(request: Request):
    """Calls the endpoint with whatever connection fields (url/method/
    headers/query_params/body/items_path) are currently in the form and
    shows the raw, unmapped response — so an operator can see a source's
    actual field names before configuring (or while configuring) the
    field-mapping table below, instead of guessing key names blind. Only
    meaningful for API-type instances; CUSTOM type has no connection
    fields to preview (its code decides everything itself)."""
    form, common = await _read_common_form(request)

    if common["adapter_type"] != "api":
        context = _error_context(common, form, "Sample fetch is only available for API-type adapters.")
        return templates.TemplateResponse(request, "adapter_form.html", context)

    try:
        config = _form_to_config("api", form)
        preview = preview_response(config)
    except Exception as e:
        context = _error_context(common, form, f"Sample fetch failed: {e}")
        return templates.TemplateResponse(request, "adapter_form.html", context)

    context = _error_context(common, form, None)
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


# Registered before the dynamic /adapters/{existing_source} route below —
# FastAPI/Starlette matches path routes in registration order, so a static
# path (a literal "test" here would otherwise be captured as
# existing_source) must come first, same reasoning as /adapters/new above
# needing to be a distinct GET path from /adapters/{source}/edit.
@router.post("/adapters")
@router.post("/adapters/{existing_source}")
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
        context = _error_context(common, form, "Source is required.")
        return templates.TemplateResponse(request, "adapter_form.html", context, status_code=400)
    if common["adapter_type"] not in _ADAPTER_CLASSES:
        context = _error_context(common, form, f"Unknown adapter type {common['adapter_type']!r}.")
        return templates.TemplateResponse(request, "adapter_form.html", context, status_code=400)

    try:
        config = _form_to_config(common["adapter_type"], form)
    except ValueError as e:
        context = _error_context(common, form, str(e))
        return templates.TemplateResponse(request, "adapter_form.html", context, status_code=400)

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
    return RedirectResponse(url=f"/adapters?{msg}", status_code=303)


@router.post("/adapters/{source}/delete")
def adapter_delete_action(source: str, conn: sqlite3.Connection = Depends(get_db)):
    deleted = delete_adapter_instance(conn, source)
    msg = f"adapter '{source}' deleted" if deleted else f"adapter '{source}' not found"
    return RedirectResponse(url=f"/adapters?{urlencode({'msg': msg})}", status_code=303)
