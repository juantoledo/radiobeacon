import ipaddress
import json
import logging
import re
import socket
import urllib.error
import urllib.request
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any
from urllib.parse import urlencode, urlsplit

from .base import AdapterItem, DataSourceAdapter, SourceReading
from .storage import DEFAULT_DB_PATH, get_connection, get_setting, get_source_fields
from .templating import safe_format
from .timeutil import to_utc, utc_now

logger = logging.getLogger(__name__)

_MAPPED_FIELDS = ("id", "title", "contents", "url", "event_key", "type", "subtype")

# The endpoint URL is operator-typed on the (unauthenticated) /adapters form
# and then fetched server-side, with the body handed back to the browser on
# the "Test"/"Sample" paths — an unrestricted request primitive otherwise.
# These bound it: http(s) only, no redirects to anywhere private, one size
# cap, one timeout. ADAPTERS_ALLOW_PRIVATE_FETCH re-opens private/loopback
# targets for a deliberate LAN or localhost feed (the running adapter honours
# it; the in-form Test/Sample always denies).
_FETCH_TIMEOUT_SECONDS = 15
_MAX_RESPONSE_BYTES = 5 * 1024 * 1024
_ALLOWED_SCHEMES = ("http", "https")


class BlockedRequestError(ValueError):
    """A fetch target that policy refuses (bad scheme, or a private address
    without ADAPTERS_ALLOW_PRIVATE_FETCH). A ValueError so the adapter's
    fetch() turns it into SourceReading(ok=False) like any other bad config."""


def _assert_public_host(host: str) -> None:
    try:
        infos = socket.getaddrinfo(host, None)
    except socket.gaierror as exc:
        raise BlockedRequestError(f"cannot resolve host {host!r}: {exc}") from exc
    for info in infos:
        addr = ipaddress.ip_address(info[4][0])
        mapped = getattr(addr, "ipv4_mapped", None)
        if mapped is not None:
            addr = mapped
        if (
            addr.is_private
            or addr.is_loopback
            or addr.is_link_local
            or addr.is_reserved
            or addr.is_multicast
            or addr.is_unspecified
        ):
            raise BlockedRequestError(
                f"host {host!r} resolves to a non-public address ({addr}); "
                "set ADAPTERS_ALLOW_PRIVATE_FETCH=true to allow a LAN feed"
            )


def _assert_scheme_allowed(url: str) -> None:
    """Cheap, no I/O — the up-front check in _fetch_json for a clear error."""
    scheme = urlsplit(url).scheme.lower()
    if scheme not in _ALLOWED_SCHEMES:
        raise BlockedRequestError(
            f"unsupported URL scheme {scheme!r} (only http/https)"
        )


def _assert_fetch_allowed(url: str, *, allow_private: bool) -> None:
    """Full check — scheme plus a DNS resolution of the host against the
    private/loopback/link-local block-list. Runs inside the opener's guard
    handler so it also fires on every redirect hop."""
    _assert_scheme_allowed(url)
    parts = urlsplit(url)
    if not parts.hostname:
        raise BlockedRequestError("URL has no host")
    if not allow_private:
        _assert_public_host(parts.hostname)


class _SsrfGuardHandler(urllib.request.BaseHandler):
    """Runs _assert_fetch_allowed on the outgoing request — and, because
    urllib feeds a redirect's new Request back through the whole handler
    chain, on every Location: hop too, so a public URL can't 302 onto
    file:// or 169.254.169.254. A redirected Request carries no
    `_allow_private` flag, so redirect targets are always checked strictly.
    handler_order 100 puts it ahead of the default HTTP(S) handlers (500)."""

    handler_order = 100

    def http_request(self, req):
        _assert_fetch_allowed(
            req.full_url, allow_private=getattr(req, "_allow_private", False)
        )
        return req

    https_request = http_request


def _build_opener() -> urllib.request.OpenerDirector:
    # A hand-built director: no FileHandler / FTPHandler / ProxyHandler, so
    # only http(s) can be opened at all, regardless of what slips past
    # _assert_fetch_allowed.
    opener = urllib.request.OpenerDirector()
    opener.add_handler(_SsrfGuardHandler())
    opener.add_handler(urllib.request.HTTPHandler())
    opener.add_handler(urllib.request.HTTPSHandler())
    opener.add_handler(urllib.request.HTTPDefaultErrorHandler())
    opener.add_handler(urllib.request.HTTPRedirectHandler())
    opener.add_handler(urllib.request.HTTPErrorProcessor())
    opener.add_handler(urllib.request.UnknownHandler())
    return opener


_OPENER = _build_opener()


def _content_uuid(item: dict[str, Any]) -> str:
    """A UUID derived from this item's own content (uuid5, not uuid4) —
    the SAME item dict always produces the SAME uuid, while two different
    items are effectively guaranteed distinct ones. Deliberately not
    random: a random {uuid} in an `id`/`event_key` template would mint a
    brand-new id on every single poll for what's really the same
    already-seen item, flooding storage with duplicates and making
    dispatcher treat it as newly-discovered forever. Used to give a
    source with genuinely no natural identifier (no id field, no usable
    timestamp either) a stable one instead."""
    fingerprint = json.dumps(item, sort_keys=True, default=str)
    return str(uuid.uuid5(uuid.NAMESPACE_URL, fingerprint))


_BARE_PLACEHOLDER_RE = re.compile(r"^\{([^{}]+)\}$")


def _bare_placeholder_field(template: str) -> str | None:
    """If `template` is exactly one placeholder and nothing else (e.g.
    "{Fecha}", not "id={Fecha}" or "{Fecha}{Magnitud}"), returns the field
    name inside the braces — else None. Used to decide whether
    field_date_format applies (see FieldMapping.resolve)."""
    match = _BARE_PLACEHOLDER_RE.match(template.strip())
    return match.group(1) if match else None


def _escape_template_braces(value: str) -> str:
    """Doubles literal `{`/`}` so a plain string (no intended
    placeholders) survives being rendered through str.format unchanged —
    used only by FieldMapping.from_dict when migrating an old `value`
    (constant) into the new template-only shape, since a constant that
    happened to contain a literal brace was never meant to be interpreted
    as a placeholder."""
    return value.replace("{", "{{").replace("}", "}}")


@dataclass
class FieldMapping:
    """How one contract field (id/title/contents/url/event_key/type/subtype)
    is derived from a raw response item. `template` is a str.format
    construction against the item's own raw fields plus a `{uuid}`
    placeholder (see _content_uuid) — deliberately the *only* way to
    configure a mapping: a bare `{Fecha}` (one placeholder, nothing else)
    is exactly a raw passthrough, a string with no `{...}` at all is
    exactly a constant, and anything in between — "Sismo M{Magnitud} -
    {RefGeografica}" — is a real construction. Three separate strategies
    (raw field / template / constant) used to exist here; they were a
    false choice, since "template" alone already covers what the other
    two did.

    `field_date_format` is the one thing a plain template can't express
    on its own: when set AND `template` is a single bare `{Name}`
    placeholder (nothing else in the string — see _bare_placeholder_field),
    that field's raw value is parsed with this strptime format and
    re-emitted as a naive ISO 8601 string (no timezone conversion) instead
    of substituted as-is. Exists so a source's own raw, unconverted
    timestamp string (e.g. CSN's "Fecha") can be normalized into a stable
    id/event_key format without ever being timezone-shifted — shifting it
    would change every existing item's id. See
    ApiAdapterConfig.date_format/source_timezone, the timezone-converted
    sibling used for source_date_time instead. Meaningless (ignored) for
    any other template shape — reformatting only makes sense for a single
    raw value on its own, not a multi-field construction."""

    template: str | None = None
    field_date_format: str | None = None

    def resolve(
        self,
        item: dict[str, Any],
        name: str,
        extra_context: dict[str, Any] | None = None,
    ) -> str | None:
        if self.template is None:
            return None
        if self.field_date_format:
            bare_field = _bare_placeholder_field(self.template)
            if bare_field is not None:
                raw = item.get(bare_field)
                if raw is None:
                    return None
                return datetime.strptime(raw, self.field_date_format).isoformat()
        # {uuid}/{raw} are fallback placeholders, not reserved words — if
        # the raw item genuinely has its own "uuid"/"raw" field, that real
        # value wins (item unpacked last/on top). {raw} is the current
        # item re-serialized as JSON, for templates that want to embed
        # the whole thing rather than pick individual fields. `extra_context`
        # carries {date}/{datetime} (this fetch's start time), the source's
        # {source_name}/{source_url}, and {response} (the entire fetched
        # response as JSON) — see ApiAdapter.fetch/_source_template_context;
        # the raw item still wins if it happens to have a same-named field.
        try:
            raw_json = json.dumps(item)
        except (TypeError, ValueError):
            raw_json = ""
        context = {"uuid": _content_uuid(item), "raw": raw_json, **(extra_context or {}), **item}
        return safe_format(self.template, f"api_adapter.{name}", **context)

    @classmethod
    def from_dict(cls, d: dict[str, Any] | None) -> "FieldMapping":
        """Accepts both the current {template, field_date_format} shape
        and the older {field, template, value, field_date_format} shape
        that predates the single-template redesign — migrating old data
        transparently on read (not by rewriting storage) so an
        already-saved adapter_instances row keeps working exactly as
        before without the operator having to redo their mapping."""
        d = d or {}
        template = d.get("template")
        if template is None and d.get("value") is not None:
            template = _escape_template_braces(d["value"])
        if template is None and d.get("field") is not None:
            template = "{" + d["field"] + "}"
        return cls(template=template, field_date_format=d.get("field_date_format"))


@dataclass
class ApiAdapterConfig:
    """The full, typed shape of an `api`-type adapter_instances.config —
    every attribute an HTTP+JSON-mapping adapter is known to need,
    encapsulated here instead of read ad hoc via scattered dict.get(...)
    calls with string-concatenated keys."""

    url: str
    method: str = "GET"
    headers: dict[str, str] = field(default_factory=dict)
    query_params: dict[str, str] = field(default_factory=dict)
    body: str | None = None
    items_path: str = ""
    mapping: dict[str, FieldMapping] = field(default_factory=dict)
    date_field: str | None = None
    date_format: str | None = None
    source_timezone: str | None = None

    @classmethod
    def from_dict(cls, config: dict[str, Any]) -> "ApiAdapterConfig":
        mapping = {
            name: FieldMapping.from_dict(raw)
            for name, raw in (config.get("mapping") or {}).items()
        }
        return cls(
            url=config["url"],
            method=config.get("method") or "GET",
            headers=config.get("headers") or {},
            query_params=config.get("query_params") or {},
            body=config.get("body"),
            items_path=config.get("items_path") or "",
            mapping=mapping,
            date_field=config.get("date_field"),
            date_format=config.get("date_format"),
            source_timezone=config.get("source_timezone"),
        )

    def mapping_for(self, name: str) -> FieldMapping:
        return self.mapping.get(name) or FieldMapping()

    def resolve_source_date_time(
        self, item: dict[str, Any], extra_context: dict[str, Any] | None = None
    ) -> datetime | None:
        """`date_field` is normally a real key in `item`, but it can also
        be set to one of FieldMapping's synthesized placeholders —
        currently only {date}/{datetime} make sense as a parseable
        timestamp (see ApiAdapter._source_template_context) — falling back
        to `extra_context` the same "real field wins" way FieldMapping.resolve
        does. Picking {datetime} with no date_format gives every item this
        fetch's own start time as its source_date_time, for sources with no
        per-item timestamp of their own."""
        raw = item.get(self.date_field) if self.date_field is not None else None
        if raw is None and self.date_field is not None and extra_context is not None:
            raw = extra_context.get(self.date_field)
        if raw is None:
            return None
        dt = datetime.strptime(raw, self.date_format) if self.date_format else datetime.fromisoformat(raw)
        return to_utc(dt, assume_tz=self.source_timezone)


def _lookup_path(data: Any, dotted_path: str) -> Any:
    """Resolves a dotted path (e.g. "result.items") against a parsed JSON
    value. An empty path returns `data` itself — the response root is the
    list. Missing keys/indices raise KeyError/TypeError, caught by the
    adapter's fetch() the same way a malformed response is caught today."""
    if not dotted_path:
        return data
    value = data
    for part in dotted_path.split("."):
        value = value[part]
    return value


def _map_item(
    item: dict[str, Any],
    cfg: ApiAdapterConfig,
    extra_context: dict[str, Any] | None = None,
) -> AdapterItem:
    fields = {
        name: cfg.mapping_for(name).resolve(item, name, extra_context) for name in _MAPPED_FIELDS
    }
    if fields["id"] is None:
        raise ValueError("mapped id is None")
    return AdapterItem(
        source_date_time=cfg.resolve_source_date_time(item, extra_context),
        raw=item,
        **fields,
    )


def _fetch_json(cfg: ApiAdapterConfig, *, allow_private: bool = False) -> Any:
    url = cfg.url
    if cfg.query_params:
        url = f"{url}?{urlencode(cfg.query_params)}"
    # Scheme up front for a clear message; the guard handler does the full
    # host check (and re-checks every redirect hop) once in flight.
    _assert_scheme_allowed(url)
    request = urllib.request.Request(
        url,
        data=cfg.body.encode("utf-8") if cfg.body else None,
        headers=cfg.headers,
        method=cfg.method,
    )
    request._allow_private = allow_private
    with _OPENER.open(request, timeout=_FETCH_TIMEOUT_SECONDS) as response:
        raw = response.read(_MAX_RESPONSE_BYTES + 1)
    if len(raw) > _MAX_RESPONSE_BYTES:
        raise BlockedRequestError(
            f"response exceeds {_MAX_RESPONSE_BYTES} bytes; refusing to buffer it"
        )
    return json.loads(raw.decode("utf-8"))


def _find_array_paths(
    data: Any, prefix: str = "", max_depth: int = 4
) -> list[tuple[str, list]]:
    """Recursively finds every array of dicts within `data` (including
    `data` itself), returning (dotted_path, array) pairs — `prefix` is ""
    for the root. Answers "how do I know this response's foreach list
    is?" without the operator having to eyeball raw JSON and hand-type a
    guess: only arrays containing at least one dict (or empty arrays) are
    surfaced, since a bare array of strings/numbers has no fields to map
    and can never be a useful `items_path` target. Does not recurse into a
    found array's own elements — items_path targets exactly one list, not
    an array nested inside its own items."""
    found: list[tuple[str, list]] = []
    if isinstance(data, list):
        if not data or any(isinstance(item, dict) for item in data):
            found.append((prefix, data))
        return found
    if isinstance(data, dict) and max_depth > 0:
        for key, value in data.items():
            child_prefix = f"{prefix}.{key}" if prefix else key
            found.extend(_find_array_paths(value, child_prefix, max_depth - 1))
    return found


def preview_response(
    config: dict[str, Any], limit: int = 5, *, allow_private: bool = False
) -> dict[str, Any]:
    """Calls the endpoint (url/method/headers/query_params/body only —
    `mapping`/`date_field` are irrelevant here)
    without mapping anything, so the UI can show an operator what a
    source's real response looks like *before* they've configured any
    field mapping. Reports both what the *currently configured*
    `items_path` resolves to, and every other array-of-dicts path found
    in the same response — so "is this actually a list, and is it the
    right one?" is answered directly instead of the operator guessing a
    dotted path blind and finding out only via a failed/empty fetch.

    Raises the same way _fetch_json does (network/HTTP errors); the
    caller (ui.routers.adapters) turns that into a user-facing error. A
    bad `items_path` (KeyError/TypeError from _lookup_path) is NOT
    raised — it's reported via `items_path_resolved=False` instead, since
    an operator mid-way through finding the right path is the expected
    case here, not a fatal error."""
    cfg = ApiAdapterConfig.from_dict(config)
    raw_response = _fetch_json(cfg, allow_private=allow_private)

    candidates = [
        {
            "path": path,
            "count": len(array),
            "sample_keys": sorted(
                {key for item in array[:limit] if isinstance(item, dict) for key in item}
            ),
        }
        for path, array in _find_array_paths(raw_response)
    ]

    try:
        resolved = _lookup_path(raw_response, cfg.items_path)
        items_path_resolved = True
    except (KeyError, TypeError, IndexError):
        resolved = None
        items_path_resolved = False

    is_list = items_path_resolved and isinstance(resolved, list)
    if is_list:
        items = resolved[:limit]
        total_count = len(resolved)
    elif items_path_resolved:
        items = [resolved]  # a real value, just not a list — shown as-is
        total_count = 1
    else:
        items = []
        total_count = 0

    return {
        "items": items,
        "total_count": total_count,
        "is_list": is_list,
        "items_path_resolved": items_path_resolved,
        "candidates": candidates,
    }


class ApiAdapter(DataSourceAdapter):
    """Generic "API type" adapter: calls an HTTP endpoint and maps the JSON
    response onto the adapter item contract, entirely from `config` (parsed
    into ApiAdapterConfig above) — no per-source Python code needed.
    `config` is parsed fresh on every fetch() call (not cached at
    construction/import time), so editing an instance's config via the UI
    takes effect on the very next poll, with no process restart required."""

    def __init__(
        self, source: str, config: dict[str, Any], *, policy=None, db_path=DEFAULT_DB_PATH
    ):
        self.source = source
        self.config = config
        self.policy = policy
        self.db_path = db_path

    def _source_template_context(
        self, cfg: ApiAdapterConfig, raw_response: Any, now: datetime
    ) -> dict[str, str]:
        """{date}/{datetime}/{source_name}/{source_url}/{response} for the
        field-mapping templates: {date}/{datetime} are this fetch's start
        time (same `now` as `fetched_at`, so every item from one fetch
        shares one timestamp — same names aiprompt_adapter's prompt
        templates already use); {source_name}/{source_url} are the
        source's display name / site URL from the `sources` table (same
        placeholders beacon & actions expose); {response} is the entire
        fetched response (pre-items_path) re-serialized as JSON for
        templates that want to embed the whole response rather than one
        item's fields. The DB lookup and the response json.dumps are each
        only done when a mapping template actually references them (the
        common case references none of them), so a source that doesn't use
        these placeholders keeps its fetch path DB-free and skips
        re-serializing a (possibly several-MB) response. Fail-soft: any DB
        trouble just yields the raw source key as {source_name} and "" as
        {source_url}, matching adapters.storage.get_source_fields' own
        unmanaged-source fallback, so a mapping is never crashed by it."""
        templates = " ".join(
            fm.template for fm in cfg.mapping.values() if fm.template is not None
        )
        context: dict[str, str] = {"date": now.date().isoformat(), "datetime": now.isoformat()}
        if "{response}" in templates:
            try:
                context["response"] = json.dumps(raw_response)
            except (TypeError, ValueError):
                context["response"] = ""
        if "{source_name}" not in templates and "{source_url}" not in templates:
            return context
        try:
            conn = get_connection(self.db_path)
            try:
                context.update(get_source_fields(conn, self.source))
                return context
            finally:
                conn.close()
        except Exception:
            logger.warning(
                "source=%s: could not resolve source display metadata for templating",
                self.source,
                exc_info=True,
            )
            context.update({"source_name": self.source, "source_url": ""})
            return context

    def _allow_private_fetch(self) -> bool:
        try:
            conn = get_connection(self.db_path)
            try:
                return get_setting(
                    "ADAPTERS_ALLOW_PRIVATE_FETCH", "false", conn=conn
                ).strip().lower() == "true"
            finally:
                conn.close()
        except Exception:
            return False

    def fetch(self) -> SourceReading:
        now = utc_now()
        try:
            cfg = ApiAdapterConfig.from_dict(self.config)
            raw_response = _fetch_json(cfg, allow_private=self._allow_private_fetch())
            raw_items = _lookup_path(raw_response, cfg.items_path)
            if isinstance(raw_items, dict):
                # items_path resolved to a single object rather than a
                # list of them — treat the whole thing as exactly one item
                # (mirrors preview_response's own `[resolved]` wrapping for
                # display) instead of iterating it as a dict, which would
                # yield its *keys* as bogus item values. This is what makes
                # a single-object-response source (e.g. "current weather"
                # style APIs, or a mapping built around {raw}/{response} —
                # see FieldMapping.resolve) work with items_path left empty.
                raw_items = [raw_items]
            elif not isinstance(raw_items, list):
                raise TypeError(
                    f"items_path {cfg.items_path!r} resolves to a "
                    f"{type(raw_items).__name__}, not a list of items"
                )
            source_context = self._source_template_context(cfg, raw_response, now)
            items = []
            for raw_item in raw_items:
                try:
                    items.append(_map_item(raw_item, cfg, source_context))
                except (KeyError, ValueError, TypeError) as e:
                    logger.error(
                        "source=%s: failed to map item, skipping: %s", self.source, e
                    )
            items.sort(
                key=lambda i: i.source_date_time or datetime.min.replace(tzinfo=timezone.utc),
                reverse=True,
            )
            logger.info("source=%s: fetched %d item(s)", self.source, len(items))
            return SourceReading(source=self.source, fetched_at=now, ok=True, data=items)
        except (OSError, urllib.error.URLError, json.JSONDecodeError, KeyError, ValueError, TypeError) as e:
            logger.error("source=%s: fetch failed: %s", self.source, e, exc_info=True)
            return SourceReading(source=self.source, fetched_at=now, ok=False, data=[], error=str(e))
