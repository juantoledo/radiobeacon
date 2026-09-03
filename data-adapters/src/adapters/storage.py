import dataclasses
import json
import logging
import os
import sqlite3
from datetime import datetime
from enum import Enum
from pathlib import Path
from string import Template
from typing import Any, Callable

from adapters.beacon_defaults import BEACON_TYPE_DEFAULT

logger = logging.getLogger(__name__)

REPO_ROOT = Path(__file__).resolve().parents[3]
DEFAULT_DB_PATH = REPO_ROOT / "storage" / "radiobeacon.db"

_audit_event_hooks: list[Callable[..., None]] = []


def register_audit_event_hook(hook: Callable[..., None]) -> None:
    """Registers a callback invoked (best-effort) after every successful
    record_audit_event() call, with the same keyword arguments
    record_audit_event() itself takes (event_type, actor, source, item_id,
    details). A hook that raises is logged and swallowed — never masks the
    caller's own operation, which has already committed by the time hooks
    run. Not called at import time by any package — callers register
    explicitly at startup, so importing this module never has network
    side effects on its own. Registering the same hook twice is a no-op."""
    if hook not in _audit_event_hooks:
        _audit_event_hooks.append(hook)

SCHEMA = """
CREATE TABLE IF NOT EXISTS items (
    source TEXT NOT NULL,
    item_id TEXT NOT NULL,
    extracted_title TEXT,
    extracted_contents TEXT,
    summary TEXT,
    url TEXT,
    event_key TEXT,
    type TEXT,
    subtype TEXT,
    transmit_policy TEXT,
    source_date_time TEXT,
    fetched_at TEXT NOT NULL,
    captured_at TEXT NOT NULL DEFAULT (datetime('now')),
    rawdata TEXT NOT NULL,
    PRIMARY KEY (source, item_id)
);
"""

# audit_log is the one durable, queryable record of pipeline events —
# unlike stdlib logging (stdout only, not persisted). Every package writes
# to it exclusively through record_audit_event() below, never directly, so
# the column set/contract stays uniform regardless of which package or
# event produced a row. `source`/`item_id` are nullable: some events (e.g.
# a transmit_policies edit) aren't about any one item.
_CREATE_AUDIT_LOG = """
CREATE TABLE IF NOT EXISTS audit_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    event_type TEXT NOT NULL,
    actor TEXT NOT NULL,
    source TEXT,
    item_id TEXT,
    details TEXT,
    recorded_at TEXT NOT NULL DEFAULT (datetime('now'))
);
"""
_CREATE_AUDIT_LOG_SOURCE_ITEM_INDEX = (
    "CREATE INDEX IF NOT EXISTS idx_audit_log_source_item ON audit_log (source, item_id);"
)
_CREATE_AUDIT_LOG_EVENT_TYPE_INDEX = (
    "CREATE INDEX IF NOT EXISTS idx_audit_log_event_type ON audit_log (event_type);"
)

# chunks is a durable, ordered record of every chunk a "chunk"-family
# action has produced — independent of whether anything was subscribed on
# MQTT to receive it. chunk_index/chunk_count preserve each chunk's
# position within its batch; UNIQUE(source, item_id, chunk_index) guards
# against a corrupt double-write within a single store_chunks() call
# (store_chunks itself deletes any prior rows for (source, item_id)
# before inserting, so a full re-chunk of the same item — a duplicate
# delivery, or a rearm whose input changed — always replaces cleanly
# rather than silently keeping stale rows alongside new ones). No
# separate index needed beyond the UNIQUE constraint's own — it already
# covers (source, item_id) as a prefix, same as items' PRIMARY KEY does.
_CREATE_CHUNKS = """
CREATE TABLE IF NOT EXISTS chunks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source TEXT NOT NULL,
    item_id TEXT NOT NULL,
    chunk_index INTEGER NOT NULL,
    chunk_count INTEGER NOT NULL,
    text TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE (source, item_id, chunk_index)
);
"""

# settings holds DB-stored overrides for the ADAPTERS_*/ACTIONS_*/DISPATCHER_*
# env vars every package otherwise reads via os.environ.get — see get_setting
# below. `key` is the exact env var name (e.g.
# "ADAPTERS_CSN_API_URL"), reusing this repo's existing
# naming convention as the row identifier instead of a separate id, same idea
# as transmit_policies' `name` PK. `is_secret` drives UI masking and stops
# set_setting from ever writing a secret's plaintext into audit_log.details.
_CREATE_SETTINGS = """
CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value TEXT,
    is_secret INTEGER NOT NULL DEFAULT 0,
    updated_at TEXT NOT NULL DEFAULT (datetime('now')),
    updated_by TEXT
);
"""

# beacon_status holds machine-written telemetry from the beacon/ package's
# transmit loop (beacon type, queue depth, last NTP check, a heartbeat) — NOT
# operator config (that's `settings`, above). beacon/ and ui/ are separate
# OS processes with no shared memory, so this table is the only way the UI
# can show "is beacon actually running / what's it doing right now"; plain
# upserts via set_beacon_status, no audit_log entries (telemetry updated
# every tick would drown out real config-change events).
_CREATE_BEACON_STATUS = """
CREATE TABLE IF NOT EXISTS beacon_status (
    key TEXT PRIMARY KEY,
    value TEXT,
    updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);
"""

# item_readiness tracks the last time an item.content_ready CloudEvent was
# published for a given item — a durable dedup marker so a process restart
# doesn't cause a duplicate publish (see actions.content_ready). Rearm
# doesn't delete prior audit_log rows, it produces new ones, so "already
# published" is a timestamp comparison (this table's published_at vs. the
# latest action.chunk.executed/action.ai.executed recorded_at), not mere
# row existence — a rearm's fresh completions naturally produce a fresh
# publish.
_CREATE_ITEM_READINESS = """
CREATE TABLE IF NOT EXISTS item_readiness (
    source TEXT NOT NULL,
    item_id TEXT NOT NULL,
    published_at TEXT NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (source, item_id)
);
"""

# sources holds per-source display metadata -- a human-readable name and a
# general site URL -- distinct from items.source (the raw internal key,
# e.g. "csn") and an individual item's own `url` (which for SENAPRED is a
# per-alert link, not a general site). Read by both actions.chunk (the
# AX.25 byte-budget clamp) and beacon.content/beacon.formatters (actual
# {source_name}/{source_url} template rendering) -- lives here, not in
# either of those sibling packages, for the same reason adapters.
# templating/adapters.ax25 do: no import exists between actions and
# beacon. Same shape as the transmit_policies table (also owned by this
# module), just keyed by `source` instead of `name`, and seeded here (see
# _ensure_sources_seeded), since every package reaches this table via the
# same get_connection() every other table here already goes through.
_CREATE_SOURCES = """
CREATE TABLE IF NOT EXISTS sources (
    source TEXT PRIMARY KEY,
    display_name TEXT NOT NULL,
    site_url TEXT,
    updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);
"""

# Seeded into `sources` on first get_connection() call, only if the table
# is empty -- the starting set an operator can then edit via sources.sh
# (list/set/delete), not by changing these constants. site_url here is
# independent of (and happens to just match, today) ADAPTERS_CSN_SITE_URL,
# which the CSN adapter uses internally for a different purpose (every
# CSN item's own `url`, since CSN has no per-earthquake detail page).
_SEED_SOURCES = (
    ("csn", "Centro Sismológico Nacional", "https://www.sismologia.cl/"),
    ("senapred", "Senapred", "https://senapred.cl/"),
)

# adapter_instances holds one row per configured adapter plugin instance —
# the DB-driven replacement for the old filesystem-discovered
# CsnAdapter/SenapredAdapter classes and their ADAPTERS_CSN_*/
# ADAPTERS_SENAPRED_* env vars. `source` is the same key space as
# items.source/sources.source. `adapter_type` selects which generic adapter
# class (adapters.api_adapter.ApiAdapter / adapters.custom_adapter.CustomAdapter)
# interprets `config` (a JSON blob whose shape depends on adapter_type — see
# those modules). `interval_seconds` NULL falls back to
# ADAPTERS_DEFAULT_INTERVAL_SECONDS, same fallback the old per-adapter
# interval lookup used. Read/written by adapters.__main__'s DB-driven loader
# and by ui's /adapters CRUD pages.
_CREATE_ADAPTER_INSTANCES = """
CREATE TABLE IF NOT EXISTS adapter_instances (
    source TEXT PRIMARY KEY,
    adapter_type TEXT NOT NULL,
    enabled INTEGER NOT NULL DEFAULT 1,
    interval_seconds INTEGER,
    config TEXT NOT NULL,
    updated_at TEXT NOT NULL DEFAULT (datetime('now')),
    updated_by TEXT
);
"""

# Seeded into `adapter_instances` on first get_connection() call, only if the
# table is empty — mirrors _ensure_sources_seeded exactly. Each seeded
# config is built (not hardcoded) by _seed_adapter_instances_config below, so
# any of the legacy ADAPTERS_CSN_*/ADAPTERS_SENAPRED_* settings this
# operator already had overridden via /config carries forward into the new
# row's JSON config instead of silently reverting to the old hardcoded
# default.
#
# A CUSTOM-type adapter_instances.config holds *only* {"code": ...} as far
# as fetch logic is concerned — no sibling keys the adapter itself reads
# (see CustomAdapter). It may additionally carry optional action-layer
# overrides, both set on the /adapters form and read by actions.ai (never by
# the adapter itself): `ai_prompt` (a per-source summarization prompt
# template) and `ai_fallback_to_title` (when true, the item's title is
# stored as the summary instead of the full extracted contents whenever AI
# can't run — a failed provider call is caught so the pipeline continues
# instead of the item hard-stopping, and the AI-disabled skip stores the
# title rather than copying extracted contents verbatim — seeded true for
# senapred, absent/false elsewhere). So unlike the old senapred module,
# which read its AWS/Cognito plumbing from env vars/config at call time,
# the generated snippet below has those values baked in as plain literals
# at seed time — _build_senapred_code() renders this template with each
# legacy ADAPTERS_SENAPRED_* value (or its hardcoded default) substituted
# in (each already pre-formatted as a Python literal via repr()), so the
# "fold forward a legacy override" property is preserved by baking the
# resolved value into the generated source text instead of into a sibling
# config key.
#
# Uses string.Template's $name substitution (see _build_senapred_code),
# not str.format — the rest of this template is full of literal `{`/`}`
# (GraphQL queries, f-strings, dict literals) that would collide with
# str.format's own brace syntax.
_SENAPRED_CUSTOM_CODE_TEMPLATE = '''"""SENAPRED CUSTOM adapter: fetches active early-warning alerts from
senapred.cl's real backend (AWS AppSync GraphQL, reached via an anonymous
Cognito Identity Pool — the same flow every visitor's browser uses when no
one is logged in). Queries both the "Alerta" and "Evento" feeds, since
senapred.cl's own /eventos/ page merges both. None of this is secret (it's
public in senapred.cl's own JS bundle)."""

import json
from html.parser import HTMLParser

IDENTITY_POOL_ID = $identity_pool_id
COGNITO_REGION = $cognito_region
COGNITO_ENDPOINT = f"https://cognito-identity.{COGNITO_REGION}.amazonaws.com/"
APPSYNC_REGION = $appsync_region
APPSYNC_HOST = $appsync_host
APPSYNC_ENDPOINT = f"https://{APPSYNC_HOST}/graphql"
ALERTA_BASE_URL = $alerta_base_url
EVENTO_BASE_URL = $evento_base_url
QUERY_LIMIT = $query_limit


def _post_json(url, headers, body):
    request = urllib.request.Request(
        url, data=body.encode("utf-8"), headers=headers, method="POST"
    )
    with urllib.request.urlopen(request, timeout=15) as response:
        return json.loads(response.read().decode("utf-8"))


def _get_anonymous_credentials(identity_pool_id, cognito_endpoint):
    identity = _post_json(
        cognito_endpoint,
        {
            "Content-Type": "application/x-amz-json-1.1",
            "X-Amz-Target": "AWSCognitoIdentityService.GetId",
        },
        json.dumps({"IdentityPoolId": identity_pool_id}),
    )
    creds = _post_json(
        cognito_endpoint,
        {
            "Content-Type": "application/x-amz-json-1.1",
            "X-Amz-Target": "AWSCognitoIdentityService.GetCredentialsForIdentity",
        },
        json.dumps({"IdentityId": identity["IdentityId"]}),
    )["Credentials"]
    return Credentials(
        access_key=creds["AccessKeyId"],
        secret_key=creds["SecretKey"],
        token=creds["SessionToken"],
    )


_ALERTAS_BY_DATE_QUERY = """
query AlertasByDate($type: String!, $sortDirection: ModelSortDirection, $limit: Int) {
  alertasByDate(type: $type, sortDirection: $sortDirection, limit: $limit) {
    items {
      id
      titulo
      contenido
      fechaHora
      autor
      isActive
      isDeleted
      type
      urlAccess
      isPrincipal
      variableRiesgo {
        nombre
        codigo
      }
    }
    nextToken
  }
}
"""

_EVENTOS_BY_DATE_QUERY = """
query EventosByDate($type: String!, $sortDirection: ModelSortDirection, $limit: Int) {
  eventosByDate(type: $type, sortDirection: $sortDirection, limit: $limit) {
    items {
      id
      titulo
      contenido
      fechaHora
      autor
      isActive
      isDeleted
      type
      urlAccess
      isPrincipal
    }
    nextToken
  }
}
"""

_HTML_SKIP_TAGS = {"script", "style", "noscript", "template", "head", "title"}
_HTML_BLOCK_TAGS = {
    "address", "article", "aside", "blockquote", "br", "dd", "div", "dl", "dt",
    "figcaption", "figure", "footer", "h1", "h2", "h3", "h4", "h5", "h6",
    "header", "hr", "li", "main", "nav", "ol", "p", "pre", "section", "table",
    "td", "th", "tr", "ul",
}


class _HtmlTextExtractor(HTMLParser):
    """Pulls human-readable text out of an HTML fragment.

    Improvement over the old `<[^>]+>` -> space regex: <script>/<style>/etc.
    bodies are dropped entirely (the regex left their JS/CSS sitting in the
    text), entity and char references are decoded by the stdlib parser
    (convert_charrefs), inline tags like <b>/<a>/<span> inject nothing so
    `<b>wor</b>d` stays "word", and only block-level tags introduce a
    boundary so `<li>a</li><li>b</li>` becomes "a b", not "ab". Malformed
    or unclosed markup is tolerated rather than mangled."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self._parts = []
        self._skip_depth = 0

    def handle_starttag(self, tag, attrs):
        if tag in _HTML_SKIP_TAGS:
            self._skip_depth += 1
        elif tag in _HTML_BLOCK_TAGS:
            self._parts.append(" ")

    def handle_startendtag(self, tag, attrs):
        if tag in _HTML_BLOCK_TAGS:
            self._parts.append(" ")

    def handle_endtag(self, tag):
        if tag in _HTML_SKIP_TAGS:
            self._skip_depth = max(0, self._skip_depth - 1)
        elif tag in _HTML_BLOCK_TAGS:
            self._parts.append(" ")

    def handle_data(self, data):
        if self._skip_depth == 0:
            self._parts.append(data)

    def text(self):
        return "".join(self._parts)


def _strip_html(raw):
    if not raw:
        return ""
    extractor = _HtmlTextExtractor()
    extractor.feed(raw)
    extractor.close()
    return " ".join(extractor.text().split())


def _query(credentials, endpoint, host, region, query, field_name, type_value, limit):
    body = json.dumps(
        {
            "query": query,
            "variables": {"type": type_value, "sortDirection": "DESC", "limit": limit},
        }
    )
    request = AWSRequest(
        method="POST", url=endpoint, data=body,
        headers={"Content-Type": "application/json", "host": host},
    )
    SigV4Auth(credentials, "appsync", region).add_auth(request)
    response = _post_json(endpoint, dict(request.headers), body)
    if "errors" in response:
        raise RuntimeError(f"AppSync GraphQL error: {response['errors']}")
    return response["data"][field_name]["items"]


def fetch(config):
    # config is unused — this CUSTOM adapter's entire config is its own
    # code (see adapters.custom_adapter.CustomAdapter); every value below
    # is a module-level constant baked in at seed time (see
    # storage._build_senapred_code), not read from config.
    credentials = _get_anonymous_credentials(IDENTITY_POOL_ID, COGNITO_ENDPOINT)
    raw_items = _query(
        credentials, APPSYNC_ENDPOINT, APPSYNC_HOST, APPSYNC_REGION,
        _ALERTAS_BY_DATE_QUERY, "alertasByDate", "Alerta", QUERY_LIMIT,
    ) + _query(
        credentials, APPSYNC_ENDPOINT, APPSYNC_HOST, APPSYNC_REGION,
        _EVENTOS_BY_DATE_QUERY, "eventosByDate", "Evento", QUERY_LIMIT,
    )

    items = []
    for item in raw_items:
        if not (item.get("isActive") and not item.get("isDeleted")):
            continue
        variable_riesgo = item.get("variableRiesgo") or {}
        item_type = item.get("type")
        url_access = item.get("urlAccess")
        base_url = EVENTO_BASE_URL if item_type == "Evento" else ALERTA_BASE_URL
        items.append(
            {
                "id": item["id"],
                "title": item["titulo"],
                "contents": _strip_html(item["contenido"]),
                "url": (base_url + url_access) if url_access else None,
                "event_key": url_access,
                "type": item_type,
                "subtype": variable_riesgo.get("nombre"),
                "transmit_policy": "informational",
                "source_date_time": to_utc(datetime.fromisoformat(item["fechaHora"])),
                "raw": item,
            }
        )
    return items
'''


# Per-adapter override of the global ACTIONS_AI_PROMPT (see
# adapters.actions_defaults.AI_PROMPT_DEFAULT) — SENAPRED reports are
# official emergency bulletins, so the default summarization prompt is
# tuned to strip exact figures (never inventing a rounder-sounding number
# in their place) in favor of qualitative language, matching how the
# beacon voice channel is meant to sound. Overridable via
# ADAPTERS_SENAPRED_AI_PROMPT, same as this seed's other ADAPTERS_SENAPRED_*
# knobs; only takes effect on a fresh install (see
# _ensure_adapter_instances_seeded) — an existing adapter_instances.senapred
# row keeps whatever ai_prompt it already has.
_SENAPRED_AI_PROMPT_DEFAULT = (
    "Resume el siguiente reporte de SENAPRED en un máximo de 3 oraciones, "
    "siguiendo estas reglas: No incluyas ninguna cifra numérica ni sus "
    "equivalentes en palabras (ej. evita tanto \"20 mm\" como \"veinte "
    "milímetros\"); usa términos cualitativos como \"varias\", \"algunas\", "
    "\"varios grados bajo cero\", etc.\n"
    "Céntrate solo en los hechos principales: qué evento ocurrió, qué "
    "región/comunas fueron afectadas, qué tipo de daños o condiciones se "
    "registraron (de forma cualitativa), el estado de rutas (si aplica), "
    "el retorno gradual de establecimientos educacionales (si aplica), y "
    "las alertas vigentes (tipo de alerta, zona y motivo).\n"
    "Usa un tono informativo y neutro, tipo titular de noticia.\n"
    "Redacta en prosa, no en formato de tabla ni de lista, ni markdown.\n"
    "Cuando menciones unidades de medida (temperatura, precipitación, "
    "viento, etc.), refiérete a ellas de forma cualitativa sin especificar "
    "cantidad (ej. \"temperaturas bajo cero\", \"precipitaciones\", "
    "\"nevadas\", \"vientos fuertes\").\n\n"
    "Texto a resumir: {extracted_contents}"
)


def _build_senapred_code(
    identity_pool_id: str,
    cognito_region: str,
    appsync_region: str,
    appsync_host: str,
    alerta_base_url: str,
    evento_base_url: str,
    query_limit: int,
) -> str:
    """Renders _SENAPRED_CUSTOM_CODE_TEMPLATE with each value substituted
    in as a Python literal (via repr()) — safe_substitute (not substitute)
    because the template also contains unrelated $-prefixed GraphQL
    variable names ($type/$sortDirection/$limit) that must pass through
    untouched, not be treated as undefined placeholders."""
    return Template(_SENAPRED_CUSTOM_CODE_TEMPLATE).safe_substitute(
        identity_pool_id=repr(identity_pool_id),
        cognito_region=repr(cognito_region),
        appsync_region=repr(appsync_region),
        appsync_host=repr(appsync_host),
        alerta_base_url=repr(alerta_base_url),
        evento_base_url=repr(evento_base_url),
        query_limit=repr(int(query_limit)),
    )


_SEED_ADAPTER_INSTANCES = (
    (
        "csn",
        "api",
        lambda get: {
            "url": get("ADAPTERS_CSN_API_URL", "https://api.gael.cloud/general/public/sismos"),
            "method": "GET",
            "headers": {"Accept": "application/json", "User-Agent": "Mozilla/5.0"},
            "items_path": "",
            "mapping": {
                "id": {"template": "{Fecha}", "field_date_format": "%Y-%m-%d %H:%M:%S"},
                "event_key": {"template": "{Fecha}", "field_date_format": "%Y-%m-%d %H:%M:%S"},
                "title": {"template": "Sismo M{Magnitud} - {RefGeografica}"},
                "contents": {
                    "template": (
                        "Sismo de magnitud {Magnitud}, profundidad {Profundidad} km, {RefGeografica}."
                    )
                },
                "url": {"template": get("ADAPTERS_CSN_SITE_URL", "https://www.sismologia.cl/")},
                "type": {"template": "Sismo"},
            },
            "date_field": "Fecha",
            "date_format": "%Y-%m-%d %H:%M:%S",
            "source_timezone": get("ADAPTERS_CSN_SOURCE_TZ", "America/Santiago"),
        },
        lambda get: int(get("ADAPTERS_CSN_INTERVAL_SECONDS", "0") or 0) or None,
    ),
    (
        "senapred",
        "custom",
        lambda get: {
            # Emergency alerts: keep the item on air even when the AI
            # summarizer's provider call fails — actions.ai then stores the
            # title as the summary instead of letting the item hard-stop.
            "ai_fallback_to_title": True,
            "ai_prompt": get("ADAPTERS_SENAPRED_AI_PROMPT", _SENAPRED_AI_PROMPT_DEFAULT),
            "code": _build_senapred_code(
                identity_pool_id=get(
                    "ADAPTERS_SENAPRED_IDENTITY_POOL_ID",
                    "us-east-1:17c696bc-53e1-49a2-991f-f1b65f752fda",
                ),
                cognito_region=get("ADAPTERS_SENAPRED_COGNITO_REGION", "us-east-1"),
                appsync_region=get("ADAPTERS_SENAPRED_APPSYNC_REGION", "us-east-1"),
                appsync_host=get(
                    "ADAPTERS_SENAPRED_APPSYNC_HOST",
                    "rz2uv7ifxbgflh2bqmp6kmh4le.appsync-api.us-east-1.amazonaws.com",
                ),
                alerta_base_url=get(
                    "ADAPTERS_SENAPRED_ALERTA_BASE_URL", "https://senapred.cl/alerta/"
                ),
                evento_base_url=get(
                    "ADAPTERS_SENAPRED_EVENTO_BASE_URL", "https://senapred.cl/evento/"
                ),
                query_limit=int(get("ADAPTERS_SENAPRED_QUERY_LIMIT", "20")),
            ),
        },
        lambda get: int(get("ADAPTERS_SENAPRED_INTERVAL_SECONDS", "0") or 0) or None,
    ),
)


# transmit_policies is the centralized repeat/interval config that
# items.transmit_policy references by name — how many times, and how far
# apart, beacon/ should put a given item on air. Owned by this module (not
# beacon/ or dispatcher/) so every package reaches it through the same
# get_connection() as every other table here. Was `dispatch_policies`,
# owned by dispatcher/, before the repeat concept moved from dispatcher's
# redelivery loop into beacon's transmit schedule.
_CREATE_TRANSMIT_POLICIES = """
CREATE TABLE IF NOT EXISTS transmit_policies (
    name TEXT PRIMARY KEY,
    repeat_times INTEGER NOT NULL,
    interval_seconds INTEGER NOT NULL,
    description TEXT
);
"""

# beacon_tx_schedule is the durable source of truth for what beacon/ still
# has to put on air and how many more times — it replaced beacon's
# in-memory drop-oldest queues, so a beacon restart no longer loses pending
# transmissions. One row per unit of transmittable content: `kind` is the
# beacon type it belongs to ("frame" | "voice"), matching BEACON_TYPE — only
# one kind is ever scheduled/drained at a time. `ref` distinguishes units
# within a kind for one item (frame = str(chunk_index), voice = "").
# `transmit_policy` is a NAME snapshot; the actual
# repeat_times/interval_seconds are resolved live every cycle via
# adapters.transmit_policy.policy_for, so editing a tier stays reactive.
# `sent_count` is stored progress; a row is deleted once
# sent_count >= repeat_times. A rearm / policy change re-inserts the row
# (PK upsert) resetting sent_count and last_transmitted_at.
_CREATE_BEACON_TX_SCHEDULE = """
CREATE TABLE IF NOT EXISTS beacon_tx_schedule (
    source              TEXT NOT NULL,
    item_id             TEXT NOT NULL,
    kind                TEXT NOT NULL,
    ref                 TEXT NOT NULL DEFAULT '',
    transmit_policy     TEXT,
    sent_count          INTEGER NOT NULL DEFAULT 0,
    last_transmitted_at TEXT,
    enqueued_event_id   TEXT,
    created_at          TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at          TEXT NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (source, item_id, kind, ref)
);
"""
_CREATE_BEACON_TX_SCHEDULE_KIND_INDEX = (
    "CREATE INDEX IF NOT EXISTS idx_beacon_tx_schedule_kind "
    "ON beacon_tx_schedule (kind);"
)


# (old_column, new_column): renames applied in order to databases created
# before a given schema change.
_COLUMN_RENAMES = (
    ("data", "rawdata"),
    ("title", "extracted_title"),
    ("contents", "extracted_contents"),
    ("url_access", "event_key"),
    ("dispatch_policy", "transmit_policy"),
)
_NEW_TEXT_COLUMNS = (
    "extracted_title",
    "extracted_contents",
    "summary",
    "url",
    "event_key",
    "type",
    "subtype",
    "transmit_policy",
    "source_date_time",
)
# Columns from an older schema — dropped on migration if present: two
# summarized_* columns replaced by the single `summary` column, and
# urgency/repeat_times/repeat_interval_seconds replaced by the single
# transmit_policy column (see adapters.transmit_policy for the centralized
# repeat/interval config it now points at).
_DROPPED_COLUMNS = (
    "summarized_title",
    "summarized_contents",
    "urgency",
    "repeat_times",
    "repeat_interval_seconds",
)


def _migrate_items_table(conn: sqlite3.Connection) -> None:
    """Patches an items table created before the current column set/names
    existed, so existing databases keep working as the schema evolves.
    Each adapter opens its own connection independently (see
    get_connection's docstring) — if two threads both open a connection
    against a still-unmigrated legacy DB at nearly the same moment, both
    can read the pre-migration column list before either commits, then
    both attempt the same ALTER TABLE. Whichever runs second gets
    sqlite3.OperationalError even though the migration step it wanted is
    already done (by the other connection) — caught per-statement below
    and treated as a no-op, not a failure, so this is self-healing
    without needing cross-connection locking."""
    columns = {row[1] for row in conn.execute("PRAGMA table_info(items)")}
    if not columns:
        return  # table doesn't exist yet; SCHEMA above creates it fresh

    for old, new in _COLUMN_RENAMES:
        if old in columns and new not in columns:
            try:
                conn.execute(f"ALTER TABLE items RENAME COLUMN {old} TO {new}")
            except sqlite3.OperationalError:
                logger.debug(
                    "items.%s already renamed to %s by another connection", old, new
                )
            columns.discard(old)
            columns.add(new)

    for column in _NEW_TEXT_COLUMNS:
        if column not in columns:
            try:
                conn.execute(f"ALTER TABLE items ADD COLUMN {column} TEXT")
            except sqlite3.OperationalError:
                logger.debug("items.%s already added by another connection", column)

    for column in _DROPPED_COLUMNS:
        if column in columns:
            try:
                conn.execute(f"ALTER TABLE items DROP COLUMN {column}")
            except sqlite3.OperationalError:
                logger.debug("items.%s already dropped by another connection", column)
            columns.discard(column)


def _migrate_transmit_policies_table(conn: sqlite3.Connection) -> None:
    """dispatch_policies was renamed to transmit_policies when the repeat
    concept moved out of dispatcher/'s redelivery loop into beacon/'s
    transmit schedule. Existing databases still have the old-named table
    with any operator-customized rows in it — that data is the source of
    truth, so it's renamed in place rather than dropped. Mirrors
    dispatcher.watcher._migrate_trigger_state_table; the per-statement
    try/except covers the concurrent-first-open race the same way
    _migrate_items_table does."""
    has_old = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'dispatch_policies'"
    ).fetchone()
    has_new = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'transmit_policies'"
    ).fetchone()
    if has_old and not has_new:
        try:
            conn.execute("ALTER TABLE dispatch_policies RENAME TO transmit_policies")
        except sqlite3.OperationalError:
            logger.debug("dispatch_policies already renamed to transmit_policies")


def _ensure_transmit_policies_table(conn: sqlite3.Connection) -> None:
    """Idempotent, and safe to call on any connection — same pattern as
    _ensure_sources_table."""
    conn.execute(_CREATE_TRANSMIT_POLICIES)


def _ensure_transmit_policies_seeded(conn: sqlite3.Connection) -> None:
    """Inserts the starting tiers only if the table is empty — mirrors
    _ensure_sources_seeded. An operator's later edits (or deletions) via
    dispatcher/policies.sh or the ui's /policies page are never overwritten
    on a subsequent get_connection() call."""
    _ensure_transmit_policies_table(conn)
    count = conn.execute("SELECT COUNT(*) FROM transmit_policies").fetchone()[0]
    if count == 0:
        from adapters.transmit_policy import SEED_POLICIES

        conn.executemany(
            "INSERT INTO transmit_policies "
            "(name, repeat_times, interval_seconds, description) VALUES (?, ?, ?, ?)",
            SEED_POLICIES,
        )
        conn.commit()


def _ensure_beacon_tx_schedule_table(conn: sqlite3.Connection) -> None:
    """Idempotent, and safe to call on any connection — the beacon TX
    helpers below call this themselves, same pattern as
    _ensure_audit_log_table."""
    conn.execute(_CREATE_BEACON_TX_SCHEDULE)
    conn.execute(_CREATE_BEACON_TX_SCHEDULE_KIND_INDEX)


def _migrate_adapter_instances_config(conn: sqlite3.Connection) -> None:
    """One-time, best-effort cleanup of existing adapter_instances.config
    rows for the dispatch_policy -> transmit_policy rename: the seeded csn
    (api) config carried a `dispatch_policy_rule` key, and the seeded
    senapred (custom) config's `code` string emitted a `"dispatch_policy"`
    item field. api_adapter/custom_adapter both read the new names with a
    fallback to the old, so this is not load-bearing — it just stops the
    old names lingering in the DB. Per-row try/except so one malformed
    config never blocks startup."""
    _ensure_adapter_instances_table(conn)
    try:
        rows = conn.execute(
            "SELECT source, adapter_type, config FROM adapter_instances"
        ).fetchall()
    except sqlite3.OperationalError:
        return

    for source, adapter_type, config_text in rows:
        try:
            cfg = json.loads(config_text)
        except (TypeError, ValueError):
            continue
        changed = False
        if isinstance(cfg, dict):
            if "dispatch_policy_rule" in cfg and "transmit_policy_rule" not in cfg:
                cfg["transmit_policy_rule"] = cfg.pop("dispatch_policy_rule")
                changed = True
            if (
                adapter_type == "custom"
                and isinstance(cfg.get("code"), str)
                and '"dispatch_policy"' in cfg["code"]
            ):
                cfg["code"] = cfg["code"].replace('"dispatch_policy"', '"transmit_policy"')
                changed = True
        if not changed:
            continue
        try:
            conn.execute(
                "UPDATE adapter_instances SET config = ? WHERE source = ?",
                (json.dumps(cfg), source),
            )
            conn.commit()
            record_audit_event(
                conn,
                event_type="adapter_instance.config_migrated",
                actor="adapters.storage",
                source=source,
                details={"rename": "dispatch_policy -> transmit_policy"},
            )
        except sqlite3.OperationalError:
            logger.debug("adapter_instances.%s config migration skipped (locked)", source)


def _backfill_senapred_ai_fallback_to_title(conn: sqlite3.Connection) -> None:
    """One-time, best-effort backfill: `ai_fallback_to_title` is seeded true
    for senapred (see _SEED_ADAPTER_INSTANCES), but the seed only runs on an
    empty table, so a database created before this key existed keeps senapred
    without it. Add it (true) only when the senapred row exists and has no
    such key — an operator who later set it explicitly (either value) is left
    untouched, and no other source is touched. Locked-DB safe, never blocks
    startup."""
    _ensure_adapter_instances_table(conn)
    try:
        row = conn.execute(
            "SELECT config FROM adapter_instances WHERE source = 'senapred'"
        ).fetchone()
    except sqlite3.OperationalError:
        return
    if row is None:
        return
    try:
        cfg = json.loads(row[0])
    except (TypeError, ValueError):
        return
    if not isinstance(cfg, dict) or "ai_fallback_to_title" in cfg:
        return
    cfg["ai_fallback_to_title"] = True
    try:
        conn.execute(
            "UPDATE adapter_instances SET config = ? WHERE source = 'senapred'",
            (json.dumps(cfg),),
        )
        conn.commit()
        record_audit_event(
            conn,
            event_type="adapter_instance.config_migrated",
            actor="adapters.storage",
            source="senapred",
            details={"backfill": "ai_fallback_to_title=true"},
        )
    except sqlite3.OperationalError:
        logger.debug("adapter_instances.senapred ai_fallback_to_title backfill skipped (locked)")


def get_connection(
    db_path: str | Path = DEFAULT_DB_PATH, *, check_same_thread: bool = True
) -> sqlite3.Connection:
    """Adapters now run as independent per-adapter loops on their own
    threads (see adapters.__main__), each opening its own connection to
    write on its own schedule — WAL mode lets those writers coexist with
    readers (e.g. query_history.sh) without blocking, and the longer
    busy_timeout (vs. Python's 5s default) gives a writer more room to
    wait out another adapter's write instead of raising "database is
    locked" on the rare occasion two fetches finish at nearly the same
    moment.

    check_same_thread=False is for a caller (e.g. ui.db.get_db) that opens
    one connection per logical unit of work but can't guarantee every
    step of that unit runs on the same OS thread — e.g. a framework whose
    threadpool executor may service a single request's dependency setup
    and its route handler body on two different worker threads. Safe
    there because the connection is still only ever touched sequentially
    within that one unit of work, never concurrently from two threads at
    once; sqlite3's same-thread check has no way to distinguish that from
    genuine concurrent cross-thread use, so it's disabled explicitly
    rather than worked around. Defaults to True (the check stays on),
    preserving every existing caller's behavior unchanged."""
    path = Path(db_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=30, check_same_thread=check_same_thread)
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.executescript(SCHEMA)
        _migrate_items_table(conn)
        _ensure_audit_log_table(conn)
        _ensure_chunks_table(conn)
        _ensure_settings_table(conn)
        _ensure_beacon_status_table(conn)
        _ensure_item_readiness_table(conn)
        _ensure_sources_table(conn)
        _ensure_sources_seeded(conn)
        _ensure_adapter_instances_table(conn)
        _ensure_adapter_instances_seeded(conn)
        _migrate_transmit_policies_table(conn)
        _ensure_transmit_policies_table(conn)
        _ensure_transmit_policies_seeded(conn)
        _ensure_beacon_tx_schedule_table(conn)
        _migrate_adapter_instances_config(conn)
        _backfill_senapred_ai_fallback_to_title(conn)
        conn.commit()
    except Exception:
        conn.close()
        raise
    logger.debug("opened sqlite connection at %s", path)
    return conn


def _ensure_audit_log_table(conn: sqlite3.Connection) -> None:
    """Idempotent, and safe to call on any connection (not just ones from
    get_connection) — record_audit_event calls this itself, so a caller
    that hand-rolls a bare sqlite3 connection (e.g. a test) doesn't need to
    separately know about this table."""
    conn.execute(_CREATE_AUDIT_LOG)
    conn.execute(_CREATE_AUDIT_LOG_SOURCE_ITEM_INDEX)
    conn.execute(_CREATE_AUDIT_LOG_EVENT_TYPE_INDEX)


def record_audit_event(
    conn: sqlite3.Connection,
    *,
    event_type: str,
    actor: str,
    source: str | None = None,
    item_id: str | None = None,
    details: dict[str, Any] | None = None,
) -> None:
    """The one contract every package writes audit rows through — same
    function, same column set, regardless of which package or which event
    produced it. `actor` identifies what wrote the row (e.g.
    "adapters.CustomAdapter", "dispatcher.watcher"). `details` is optional free-form JSON (reuses
    _json_default for datetime/dataclass/Enum values, same as rawdata).
    After the row commits, every hook registered via
    register_audit_event_hook() is invoked with these same arguments —
    see that function for the contract (best-effort, never raises)."""
    _ensure_audit_log_table(conn)
    conn.execute(
        "INSERT INTO audit_log (event_type, actor, source, item_id, details) "
        "VALUES (?, ?, ?, ?, ?)",
        (
            event_type,
            actor,
            source,
            item_id,
            json.dumps(details, default=_json_default) if details is not None else None,
        ),
    )
    conn.commit()

    for hook in _audit_event_hooks:
        try:
            hook(
                event_type=event_type,
                actor=actor,
                source=source,
                item_id=item_id,
                details=details,
            )
        except Exception:
            logger.error("audit event hook %r failed", hook, exc_info=True)


def _ensure_chunks_table(conn: sqlite3.Connection) -> None:
    """Idempotent, and safe to call on any connection — store_chunks calls
    this itself, same pattern as _ensure_audit_log_table."""
    conn.execute(_CREATE_CHUNKS)


def store_chunks(conn: sqlite3.Connection, chunks: list[dict[str, Any]]) -> int:
    """Persists chunks (the exact dicts actions.chunk.ChunkAction.run()
    builds) in order. Querying back with `ORDER BY chunk_index` always
    reconstructs the original sequence — insertion order alone isn't
    relied on for correctness, chunk_index is the authoritative position.

    Replaces any existing chunks for each (source, item_id) pair in this
    batch before inserting the new ones — deliberately NOT insert-or-
    ignore-and-keep-the-old-rows. actions.chunk's input can now change
    between runs (raw extracted_contents vs. an AI summary that appears
    later, or changes on a rearm), unlike historically when
    extracted_contents was immutable and re-chunking the same item always
    produced byte-identical output. With insert-or-ignore, a later,
    correct re-chunk would silently fail to overwrite stale rows sharing
    a chunk_index with an earlier run — exactly the bug this replaced
    (confirmed live: a raw-content chunk run that raced ahead of AI
    settling left permanently stale chunks even after a correct
    summary-based re-chunk followed moments later). Still fully
    idempotent for a genuine duplicate delivery of the same batch — the
    end state is identical either way.

    Returns the number of rows written this call (not a "newly stored"
    count anymore, since nothing is silently skipped now)."""
    _ensure_chunks_table(conn)
    pairs = {(chunk["source"], chunk["item_id"]) for chunk in chunks}
    for source, item_id in pairs:
        conn.execute("DELETE FROM chunks WHERE source = ? AND item_id = ?", (source, item_id))
    stored = 0
    for chunk in chunks:
        cursor = conn.execute(
            "INSERT INTO chunks (source, item_id, chunk_index, chunk_count, text) "
            "VALUES (?, ?, ?, ?, ?)",
            (
                chunk["source"],
                chunk["item_id"],
                chunk["chunk_index"],
                chunk["chunk_count"],
                chunk["text"],
            ),
        )
        stored += cursor.rowcount
    conn.commit()
    return stored


def store_summary(conn: sqlite3.Connection, source: str, item_id: str, summary: str) -> bool:
    """Writes an AI-generated summary back onto an existing item (see
    actions.ai.AiAction). Mirrors dispatcher.override.override_item's
    shape: a plain UPDATE, returns whether a row was actually updated
    (False if source/item_id doesn't match any stored item)."""
    cursor = conn.execute(
        "UPDATE items SET summary = ? WHERE source = ? AND item_id = ?",
        (summary, source, item_id),
    )
    conn.commit()
    return cursor.rowcount > 0


def _ensure_settings_table(conn: sqlite3.Connection) -> None:
    """Idempotent, and safe to call on any connection — get_setting/
    set_setting/list_settings/delete_setting all call this themselves, same
    pattern as _ensure_audit_log_table/_ensure_chunks_table."""
    conn.execute(_CREATE_SETTINGS)


def get_setting(
    key: str,
    default: str | None = None,
    *,
    conn: sqlite3.Connection | None = None,
    db_path: str | Path | None = None,
    env_fallback: bool = True,
) -> str | None:
    """Drop-in replacement for os.environ.get(key, default) at every config
    call site across adapters/actions/dispatcher — callers keep doing their
    own int()/float()/bool() casting around the returned string, exactly as
    before. Resolution order: a settings row with a non-NULL value -> the
    env var -> default.

    env_fallback=False skips the os.environ.get step entirely (falling
    straight through to `default` when no DB row exists) — for a key that
    is deliberately DB-only and must never be satisfiable by a same-named
    env var (e.g. the BEACON_* identity fields in ui.beacon, which are
    edited only through the UI). Every other caller keeps the default
    True, preserving today's DB -> env -> default behavior unchanged.

    Pass conn to reuse an already-open connection (e.g. inside Action.run(),
    which already receives one per message) rather than opening a new one on
    a per-message-hot path. If conn is None (module-level/import-time
    callers, which have no connection to reuse), opens+closes a short-lived
    one via get_connection(db_path) — safe to call at adapter import time,
    since get_connection() creates/migrates the full schema (including
    `settings`) idempotently on first open, regardless of which process
    happens to be the first to ever connect.

    db_path defaults to None (resolved to DEFAULT_DB_PATH inside the
    function body), not `= DEFAULT_DB_PATH` in the signature — a default
    parameter value is bound once, at module-import time, so a signature
    default would silently ignore a later `monkeypatch.setattr(storage_module,
    "DEFAULT_DB_PATH", ...)` in a test that omits db_path/conn (as every
    module-level/no-conn caller, e.g. dispatcher.mq_publisher, does)."""
    owns_conn = conn is None
    if owns_conn:
        conn = get_connection(DEFAULT_DB_PATH if db_path is None else db_path)
    try:
        _ensure_settings_table(conn)
        row = conn.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
    finally:
        if owns_conn:
            conn.close()
    if row is not None and row[0] is not None:
        return row[0]
    if not env_fallback:
        return default
    return os.environ.get(key, default)


def set_setting(
    conn: sqlite3.Connection,
    key: str,
    value: str | None,
    *,
    is_secret: bool = False,
    actor: str = "ui.config",
) -> None:
    """Upserts one settings row and records a "setting.changed" audit event.
    When is_secret is True, the audit event's `details` deliberately omits
    `value` (only {"key": key, "is_secret": True}) — audit_log is queryable
    via the UI's /audit page, so writing a secret's plaintext there would
    defeat get_setting/set_setting's whole mask-on-read design."""
    _ensure_settings_table(conn)
    conn.execute(
        "INSERT INTO settings (key, value, is_secret, updated_at, updated_by) "
        "VALUES (?, ?, ?, datetime('now'), ?) "
        "ON CONFLICT(key) DO UPDATE SET "
        "value = excluded.value, is_secret = excluded.is_secret, "
        "updated_at = excluded.updated_at, updated_by = excluded.updated_by",
        (key, value, int(is_secret), actor),
    )
    conn.commit()
    details: dict[str, Any] = {"key": key, "is_secret": is_secret}
    if not is_secret:
        details["value"] = value
    record_audit_event(conn, event_type="setting.changed", actor=actor, details=details)


def delete_setting(conn: sqlite3.Connection, key: str, *, actor: str = "ui.config") -> bool:
    """Clears a DB override, reverting the key back to its env var/hardcoded
    default. Returns whether a row actually existed."""
    _ensure_settings_table(conn)
    cursor = conn.execute("DELETE FROM settings WHERE key = ?", (key,))
    conn.commit()
    deleted = cursor.rowcount > 0
    if deleted:
        record_audit_event(
            conn, event_type="setting.reset", actor=actor, details={"key": key}
        )
    return deleted


def list_settings(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    """All settings rows (key, value, is_secret, updated_at, updated_by),
    ordered by key — the UI's source of "which keys currently have a DB
    override"."""
    _ensure_settings_table(conn)
    return conn.execute("SELECT * FROM settings ORDER BY key").fetchall()


def _ensure_beacon_status_table(conn: sqlite3.Connection) -> None:
    """Idempotent, and safe to call on any connection — set_beacon_status/
    get_beacon_status/list_beacon_status all call this themselves, same
    pattern as _ensure_settings_table."""
    conn.execute(_CREATE_BEACON_STATUS)


def set_beacon_status(conn: sqlite3.Connection, key: str, value: str | None) -> None:
    """Upserts one beacon_status row. Plain telemetry write — no audit_log
    entry (see beacon_status's schema comment above) and no actor, unlike
    set_setting, since this is never a human-initiated config change."""
    _ensure_beacon_status_table(conn)
    conn.execute(
        "INSERT INTO beacon_status (key, value, updated_at) "
        "VALUES (?, ?, datetime('now')) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at",
        (key, value),
    )
    conn.commit()


def get_beacon_status(conn: sqlite3.Connection, key: str) -> str | None:
    _ensure_beacon_status_table(conn)
    row = conn.execute("SELECT value FROM beacon_status WHERE key = ?", (key,)).fetchone()
    return row[0] if row is not None else None


def list_beacon_status(conn: sqlite3.Connection) -> dict[str, str]:
    """All beacon_status rows as a plain {key: value} dict — the UI's
    /beacon status section reads this to show current slot, queue depths,
    last NTP check, etc. without needing to know the individual keys."""
    _ensure_beacon_status_table(conn)
    rows = conn.execute("SELECT key, value FROM beacon_status").fetchall()
    return {row[0]: row[1] for row in rows}


def _ensure_item_readiness_table(conn: sqlite3.Connection) -> None:
    """Idempotent, and safe to call on any connection — mark_item_ready_published/
    get_item_ready_published_at call this themselves, same pattern as
    _ensure_settings_table."""
    conn.execute(_CREATE_ITEM_READINESS)


def mark_item_ready_published(conn: sqlite3.Connection, source: str, item_id: str) -> None:
    """Upserts item_readiness.published_at to now — called right after
    actions.content_ready publishes an item.content_ready CloudEvent for
    (source, item_id), so the next poll tick (or a later restart) doesn't
    republish for the same completion."""
    _ensure_item_readiness_table(conn)
    conn.execute(
        "INSERT INTO item_readiness (source, item_id, published_at) "
        "VALUES (?, ?, datetime('now')) "
        "ON CONFLICT(source, item_id) DO UPDATE SET published_at = excluded.published_at",
        (source, item_id),
    )
    conn.commit()


def get_item_ready_published_at(conn: sqlite3.Connection, source: str, item_id: str) -> str | None:
    _ensure_item_readiness_table(conn)
    row = conn.execute(
        "SELECT published_at FROM item_readiness WHERE source = ? AND item_id = ?",
        (source, item_id),
    ).fetchone()
    return row[0] if row is not None else None


_BEACON_TX_SCHEDULE_COLUMNS = (
    "source", "item_id", "kind", "ref", "transmit_policy",
    "sent_count", "last_transmitted_at", "enqueued_event_id",
    "created_at", "updated_at",
)


def _tx_schedule_row_to_dict(row: tuple) -> dict[str, Any]:
    """adapters.storage's connections keep the default tuple row_factory
    (see _adapter_instance_row_to_dict) — build a plain dict so beacon's
    transmit loop gets row["kind"] access regardless of the connection."""
    return dict(zip(_BEACON_TX_SCHEDULE_COLUMNS, row))


def add_tx_schedule_unit(
    conn: sqlite3.Connection,
    source: str,
    item_id: str,
    kind: str,
    ref: str,
    transmit_policy: str | None,
    enqueued_event_id: str | None,
    *,
    max_size: int | None = None,
) -> None:
    """Inserts (or resets, on a rearm / policy change) one transmittable
    unit for beacon/. The PK is (source, item_id, kind, ref), so a repeat
    delivery of the same item.content_ready — or a genuine rearm with a new
    CloudEvent id — upserts the row back to sent_count=0,
    last_transmitted_at=NULL and refreshes the policy name + event id. When
    `max_size` is given, trims the oldest rows of this `kind` beyond the cap
    (by created_at), preserving beacon's old drop-oldest queue behavior."""
    _ensure_beacon_tx_schedule_table(conn)
    conn.execute(
        "INSERT INTO beacon_tx_schedule "
        "(source, item_id, kind, ref, transmit_policy, enqueued_event_id) "
        "VALUES (?, ?, ?, ?, ?, ?) "
        "ON CONFLICT (source, item_id, kind, ref) DO UPDATE SET "
        "transmit_policy = excluded.transmit_policy, "
        "enqueued_event_id = excluded.enqueued_event_id, "
        "sent_count = 0, last_transmitted_at = NULL, "
        "updated_at = datetime('now')",
        (source, item_id, kind, ref, transmit_policy, enqueued_event_id),
    )
    if max_size is not None and max_size > 0:
        conn.execute(
            "DELETE FROM beacon_tx_schedule WHERE kind = ? AND rowid NOT IN ("
            "SELECT rowid FROM beacon_tx_schedule WHERE kind = ? "
            "ORDER BY created_at DESC, rowid DESC LIMIT ?)",
            (kind, kind, max_size),
        )
    conn.commit()


def schedule_retransmit(conn: sqlite3.Connection, source: str, item_id: str) -> dict[str, Any]:
    """Ad-hoc re-enqueue of an already-processed item's beacon transmission
    (the ui item detail page's "Re-transmit" action on a beacon.voice.*/
    beacon.frame.*/beacon.tx.retired audit row), as opposed to a fresh
    item.content_ready publish. Schedules fresh beacon_tx_schedule row(s)
    for the CURRENTLY configured BEACON_TYPE — one kind="voice" row, or one
    kind="frame" row per existing `chunks` row (mirrors
    beacon.__main__._handle_content_ready_event's own scheduling shape) —
    which beacon's own already-running transmit loop picks up and sends on
    its next tick via the normal _transmit_voice_unit/_transmit_frame_unit
    path. No TTS/audio synthesis happens here — this only writes DB state,
    keeping this callable from ui (which has no TTS binaries) without any
    new dependency on the beacon package. Returns {"kind": None,
    "scheduled": 0} if the item doesn't exist — nothing to retransmit."""
    policy_row = conn.execute(
        "SELECT transmit_policy FROM items WHERE source = ? AND item_id = ?",
        (source, item_id),
    ).fetchone()
    if policy_row is None:
        return {"kind": None, "scheduled": 0}
    transmit_policy = policy_row[0]

    beacon_type = (get_setting("BEACON_TYPE", BEACON_TYPE_DEFAULT, conn=conn) or "").strip().lower()
    if beacon_type not in ("voice", "frame"):
        beacon_type = BEACON_TYPE_DEFAULT

    if beacon_type == "frame":
        rows = conn.execute(
            "SELECT chunk_index FROM chunks WHERE source = ? AND item_id = ? ORDER BY chunk_index",
            (source, item_id),
        ).fetchall()
        for (chunk_index,) in rows:
            add_tx_schedule_unit(
                conn, source, item_id, "frame", str(chunk_index), transmit_policy, None
            )
        scheduled = len(rows)
    else:
        add_tx_schedule_unit(conn, source, item_id, "voice", "", transmit_policy, None)
        scheduled = 1

    record_audit_event(
        conn,
        event_type="beacon.retransmit.enqueued",
        actor="ui.retransmit",
        source=source,
        item_id=item_id,
        details={"beacon_type": beacon_type, "scheduled": scheduled},
    )
    return {"kind": beacon_type, "scheduled": scheduled}


def due_tx_schedule_rows(conn: sqlite3.Connection, kind: str) -> list[dict[str, Any]]:
    """Every beacon_tx_schedule row of this `kind`, oldest first. Due-ness
    (against the row's live policy interval) and retirement are decided by
    the caller — this keeps the SQL policy-agnostic, same style as the old
    dispatcher.watcher.dispatch_due_items."""
    _ensure_beacon_tx_schedule_table(conn)
    rows = conn.execute(
        f"SELECT {', '.join(_BEACON_TX_SCHEDULE_COLUMNS)} FROM beacon_tx_schedule "
        "WHERE kind = ? ORDER BY created_at, rowid",
        (kind,),
    ).fetchall()
    return [_tx_schedule_row_to_dict(row) for row in rows]


def record_tx_schedule_sent(
    conn: sqlite3.Connection,
    source: str,
    item_id: str,
    kind: str,
    ref: str,
    *,
    now_iso: str,
    retire: bool,
) -> None:
    """Marks one unit transmitted: either bump sent_count + stamp
    last_transmitted_at, or delete the row when its repeat budget is spent.
    Every *attempt* counts (success or failure), mirroring beacon's old
    _try_transmit_* which recorded a *_transmit_failed audit row and moved
    on."""
    _ensure_beacon_tx_schedule_table(conn)
    if retire:
        conn.execute(
            "DELETE FROM beacon_tx_schedule "
            "WHERE source = ? AND item_id = ? AND kind = ? AND ref = ?",
            (source, item_id, kind, ref),
        )
    else:
        conn.execute(
            "UPDATE beacon_tx_schedule SET sent_count = sent_count + 1, "
            "last_transmitted_at = ?, updated_at = datetime('now') "
            "WHERE source = ? AND item_id = ? AND kind = ? AND ref = ?",
            (now_iso, source, item_id, kind, ref),
        )
    conn.commit()


def count_tx_schedule_by_kind(conn: sqlite3.Connection) -> dict[str, int]:
    """{kind: pending-row-count} for beacon's heartbeat telemetry."""
    _ensure_beacon_tx_schedule_table(conn)
    return {
        kind: count
        for kind, count in conn.execute(
            "SELECT kind, COUNT(*) FROM beacon_tx_schedule GROUP BY kind"
        )
    }


def has_pending_tx_schedule(conn: sqlite3.Connection, kind: str) -> bool:
    """Whether any row of this `kind` is still pending — beacon uses this
    to decide whether to pre-switch radio services ahead of a slot."""
    _ensure_beacon_tx_schedule_table(conn)
    return (
        conn.execute(
            "SELECT EXISTS(SELECT 1 FROM beacon_tx_schedule WHERE kind = ?)", (kind,)
        ).fetchone()[0]
        == 1
    )


def delete_tx_schedule_for_item(conn: sqlite3.Connection, source: str, item_id: str) -> None:
    """Drops every pending transmit unit for an item — called when the item
    itself is deleted (see ui.dev_ops.delete_item)."""
    _ensure_beacon_tx_schedule_table(conn)
    conn.execute(
        "DELETE FROM beacon_tx_schedule WHERE source = ? AND item_id = ?",
        (source, item_id),
    )
    conn.commit()


def delete_tx_schedule_other_kinds(conn: sqlite3.Connection, keep_kind: str) -> int:
    """Drops every pending transmit row whose `kind` is not `keep_kind` — beacon
    schedules and transmits a single kind (BEACON_TYPE) at a time, so rows left
    over from a previous type would otherwise sit dormant forever. Returns the
    number of rows removed."""
    _ensure_beacon_tx_schedule_table(conn)
    cur = conn.execute("DELETE FROM beacon_tx_schedule WHERE kind != ?", (keep_kind,))
    conn.commit()
    return cur.rowcount


def _ensure_sources_table(conn: sqlite3.Connection) -> None:
    """Idempotent, and safe to call on any connection — same pattern as
    _ensure_settings_table/_ensure_item_readiness_table."""
    conn.execute(_CREATE_SOURCES)


def _ensure_sources_seeded(conn: sqlite3.Connection) -> None:
    """Inserts _SEED_SOURCES only if the table is empty — mirrors
    _ensure_transmit_policies_seeded exactly, so a fresh database starts
    with csn/senapred already present, but an operator's later edits (or
    deletions) via sources.sh are never overwritten on a subsequent
    get_connection() call."""
    _ensure_sources_table(conn)
    count = conn.execute("SELECT COUNT(*) FROM sources").fetchone()[0]
    if count == 0:
        conn.executemany(
            "INSERT INTO sources (source, display_name, site_url) VALUES (?, ?, ?)",
            _SEED_SOURCES,
        )
        conn.commit()


def get_source_fields(conn: sqlite3.Connection, source: str) -> dict[str, str]:
    """{"source_name": ..., "source_url": ...} for template placeholders
    (see beacon.content.resolve_item_fields / actions.chunk.ChunkAction).
    A source with no row (never seeded, or deleted via sources.sh) falls
    back to its own raw key as source_name and "" as source_url, rather
    than erroring — fail-soft, same spirit as adapters.templating.
    safe_format: a template referencing {source_name} for an unmanaged
    source still renders something sane instead of crashing."""
    _ensure_sources_table(conn)
    row = conn.execute(
        "SELECT display_name, site_url FROM sources WHERE source = ?", (source,)
    ).fetchone()
    if row is None:
        return {"source_name": source, "source_url": ""}
    display_name, site_url = row
    return {"source_name": display_name, "source_url": site_url or ""}


def set_source(
    conn: sqlite3.Connection, source: str, display_name: str, site_url: str | None = None
) -> None:
    """Creates or replaces a source's display metadata — the actual
    "manage these directly" surface (see data-adapters/sources.py).
    Mirrors adapters.transmit_policy.set_policy's upsert-then-audit shape."""
    _ensure_sources_table(conn)
    conn.execute(
        "INSERT INTO sources (source, display_name, site_url, updated_at) "
        "VALUES (?, ?, ?, datetime('now')) "
        "ON CONFLICT (source) DO UPDATE SET "
        "display_name = excluded.display_name, "
        "site_url = excluded.site_url, "
        "updated_at = excluded.updated_at",
        (source, display_name, site_url),
    )
    conn.commit()
    record_audit_event(
        conn,
        event_type="source.set",
        actor="data-adapters.sources",
        source=source,
        details={"display_name": display_name, "site_url": site_url},
    )


def list_sources(conn: sqlite3.Connection) -> list[tuple[str, str, str | None]]:
    """Returns (source, display_name, site_url) rows, ordered by source."""
    _ensure_sources_table(conn)
    return conn.execute(
        "SELECT source, display_name, site_url FROM sources ORDER BY source"
    ).fetchall()


def delete_source(conn: sqlite3.Connection, source: str) -> bool:
    """Returns whether a row was actually deleted (False if unknown).
    Mirrors adapters.transmit_policy.delete_policy."""
    _ensure_sources_table(conn)
    cursor = conn.execute("DELETE FROM sources WHERE source = ?", (source,))
    conn.commit()
    if cursor.rowcount > 0:
        record_audit_event(
            conn, event_type="source.deleted", actor="data-adapters.sources", source=source,
        )
    return cursor.rowcount > 0


def _ensure_adapter_instances_table(conn: sqlite3.Connection) -> None:
    """Idempotent, and safe to call on any connection — same pattern as
    _ensure_sources_table."""
    conn.execute(_CREATE_ADAPTER_INSTANCES)


def _ensure_adapter_instances_seeded(conn: sqlite3.Connection) -> None:
    """Inserts a csn (api) and senapred (custom) row only if the table is
    empty — mirrors _ensure_sources_seeded exactly. Each seed's config is
    built by calling its lambda with a `get(key, default)` helper reading
    adapters.storage.get_setting(key, default, conn=conn) — so if this
    operator's live DB already had any of the legacy ADAPTERS_CSN_*/
    ADAPTERS_SENAPRED_* settings rows (set via the old /config groups),
    those values carry into the seeded row's JSON config instead of
    silently reverting to the hardcoded default."""
    _ensure_adapter_instances_table(conn)
    count = conn.execute("SELECT COUNT(*) FROM adapter_instances").fetchone()[0]
    if count != 0:
        return

    def get(key: str, default: str) -> str:
        return get_setting(key, default, conn=conn) or default

    for source, adapter_type, config_builder, interval_builder in _SEED_ADAPTER_INSTANCES:
        conn.execute(
            "INSERT INTO adapter_instances "
            "(source, adapter_type, enabled, interval_seconds, config) "
            "VALUES (?, ?, 1, ?, ?)",
            (
                source,
                adapter_type,
                interval_builder(get),
                json.dumps(config_builder(get)),
            ),
        )
    conn.commit()


_ADAPTER_INSTANCE_COLUMNS = (
    "source", "adapter_type", "enabled", "interval_seconds", "config", "updated_at", "updated_by",
)


def _adapter_instance_row_to_dict(row: tuple) -> dict[str, Any]:
    """adapters.storage's connections don't set row_factory = sqlite3.Row
    (a connection-wide setting some callers, e.g. this package's own
    __main__.py, rely on staying plain tuples elsewhere on the same
    connection — see get_source_fields's tuple-unpacking style above) —
    so these two reads build a plain dict themselves instead, giving every
    caller ergonomic row["key"] access regardless of the connection's own
    row_factory."""
    return dict(zip(_ADAPTER_INSTANCE_COLUMNS, row))


def get_adapter_instance(conn: sqlite3.Connection, source: str) -> dict[str, Any] | None:
    _ensure_adapter_instances_table(conn)
    row = conn.execute(
        f"SELECT {', '.join(_ADAPTER_INSTANCE_COLUMNS)} FROM adapter_instances WHERE source = ?",
        (source,),
    ).fetchone()
    return _adapter_instance_row_to_dict(row) if row is not None else None


def list_adapter_instances(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    """All adapter_instances rows, ordered by source — read by both
    adapters.__main__'s DB-driven loader (enabled rows only, filtered by the
    caller) and the ui's /adapters list page (every row)."""
    _ensure_adapter_instances_table(conn)
    rows = conn.execute(
        f"SELECT {', '.join(_ADAPTER_INSTANCE_COLUMNS)} FROM adapter_instances ORDER BY source"
    ).fetchall()
    return [_adapter_instance_row_to_dict(row) for row in rows]


def set_adapter_instance(
    conn: sqlite3.Connection,
    source: str,
    adapter_type: str,
    config: dict[str, Any],
    *,
    enabled: bool = True,
    interval_seconds: int | None = None,
    actor: str = "ui.adapters",
) -> None:
    """Creates or replaces one adapter_instances row. Mirrors
    set_source's upsert-then-audit shape. `config` is passed as a dict and
    serialized here (not by the caller) so every writer produces
    consistently-formatted JSON."""
    _ensure_adapter_instances_table(conn)
    conn.execute(
        "INSERT INTO adapter_instances "
        "(source, adapter_type, enabled, interval_seconds, config, updated_at, updated_by) "
        "VALUES (?, ?, ?, ?, ?, datetime('now'), ?) "
        "ON CONFLICT(source) DO UPDATE SET "
        "adapter_type = excluded.adapter_type, "
        "enabled = excluded.enabled, "
        "interval_seconds = excluded.interval_seconds, "
        "config = excluded.config, "
        "updated_at = excluded.updated_at, "
        "updated_by = excluded.updated_by",
        (source, adapter_type, int(enabled), interval_seconds, json.dumps(config), actor),
    )
    conn.commit()
    record_audit_event(
        conn,
        event_type="adapter_instance.set",
        actor=actor,
        source=source,
        details={"adapter_type": adapter_type, "enabled": enabled, "interval_seconds": interval_seconds},
    )


def delete_adapter_instance(conn: sqlite3.Connection, source: str, *, actor: str = "ui.adapters") -> bool:
    """Returns whether a row was actually deleted. Mirrors delete_source —
    leaves historical `items` rows and the `sources` display-metadata row
    untouched, same as deleting a transmit_policy doesn't delete items."""
    _ensure_adapter_instances_table(conn)
    cursor = conn.execute("DELETE FROM adapter_instances WHERE source = ?", (source,))
    conn.commit()
    if cursor.rowcount > 0:
        record_audit_event(
            conn, event_type="adapter_instance.deleted", actor=actor, source=source,
        )
    return cursor.rowcount > 0


def _json_default(obj: Any) -> Any:
    if dataclasses.is_dataclass(obj) and not isinstance(obj, type):
        return dataclasses.asdict(obj)
    if isinstance(obj, datetime):
        return obj.isoformat()
    if isinstance(obj, Enum):
        return obj.value
    raise TypeError(f"Object of type {type(obj).__name__} is not JSON serializable")


def _as_text(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.isoformat()
    return str(value)


def store_reading(conn: sqlite3.Connection, reading: Any) -> int:
    """Stores each item in reading.data, keyed by its `id`. Items without an
    `id` are skipped. Uniqueness (one row per source+id) is enforced by the
    items table's primary key.

    Items are treated as immutable once stored: an already-known id is
    left completely untouched — no column is refreshed, no write happens
    at all — only genuinely new items get inserted. This assumes an
    adapter never reuses an id for content that changes over time (true
    for SENAPRED: it publishes a new item, with a new id, for every update
    to an event rather than mutating an existing one — see `event_key`,
    which is what groups those separate immutable items into one event's
    history). If a future adapter's items legitimately do change under the
    same id, this function would need revisiting for that adapter.

    `title`, `contents`, `url`, `event_key`, `type`, `subtype`, and
    `source_date_time` are read from the item via duck typing (each
    optional) — this is the generic item contract adapters may implement,
    on top of the raw JSON serialization (`rawdata`) that's always stored
    regardless. `title`/`contents` land in
    `extracted_title`/`extracted_contents`.

    `event_key` (where an adapter has one) is the item's grouping/thread
    key — e.g. for SENAPRED, several items over time (declared, monitored,
    modified, cancelled) share the same `event_key` (SENAPRED's own
    `url_access`), forming one event's history. Persisting it lets that
    history be reconstructed later from already-stored rows:
    `WHERE event_key = ? ORDER BY source_date_time`.

    `type`/`subtype` are a generic two-level category — e.g. for SENAPRED,
    `type` is "Alerta"/"Evento" (which of its two separate GraphQL feeds
    an item came from) and `subtype` is the risk category (e.g.
    "Hidrometeorologico"). Any adapter with a similar broad/fine category
    split can use the same two columns.

    `transmit_policy` (where an adapter has one) names how often beacon/
    should put this item on air — e.g. "urgent" or "informational".
    Meaningless to this package: it's a soft reference (not a SQL FOREIGN
    KEY) to the `name` column of the `transmit_policies` table (also owned
    by this module), which centralizes the actual repeat count/interval
    config a name maps to (see adapters.transmit_policy). A row with no
    `transmit_policy` (adapter doesn't implement it, value missing, or the
    name doesn't exist in `transmit_policies`) falls back to the default
    policy.

    `summary`, like `transmit_policy`, is never touched here — adapters
    only propose an initial value (or leave it NULL/unset); a separate
    actor updates it afterward. `summary` starts NULL and stays that way
    until a separate actor sets it.
    `transmit_policy` starts at whatever the adapter proposed and can be
    overridden afterward by a human/UI (via dispatcher/override_item.py) —
    unlike the rest of the row, this column is not meant to be
    immutable-forever, only adapter-untouched-after-insert.

    Returns the number of newly stored (previously unseen) items."""
    stored = 0
    skipped_no_id = 0
    failed = 0
    items = reading.data if isinstance(reading.data, list) else []
    for item in items:
        item_id = getattr(item, "id", None)
        if item_id is None:
            skipped_no_id += 1
            continue
        try:
            cursor = conn.execute(
                "INSERT OR IGNORE INTO items "
                "(source, item_id, extracted_title, extracted_contents, "
                "summary, url, event_key, type, subtype, transmit_policy, "
                "source_date_time, fetched_at, rawdata) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    reading.source,
                    item_id,
                    _as_text(getattr(item, "title", None)),
                    _as_text(getattr(item, "contents", None)),
                    None,  # summary — never set here, populated later by a separate actor
                    _as_text(getattr(item, "url", None)),
                    _as_text(getattr(item, "event_key", None)),
                    _as_text(getattr(item, "type", None)),
                    _as_text(getattr(item, "subtype", None)),
                    _as_text(getattr(item, "transmit_policy", None)),
                    _as_text(getattr(item, "source_date_time", None)),
                    reading.fetched_at.isoformat(),
                    json.dumps(item, default=_json_default),
                ),
            )
            if cursor.rowcount:
                record_audit_event(
                    conn,
                    event_type="item.stored",
                    actor="adapters.storage",
                    source=reading.source,
                    item_id=item_id,
                )
            stored += cursor.rowcount
        except Exception:
            # One malformed/unexpected item must never discard every item
            # processed earlier in this same batch — those are still
            # pending in this transaction and get committed below along
            # with everything else; only this one item is skipped.
            failed += 1
            logger.error(
                "source=%s: failed to store item_id=%s, skipping",
                reading.source,
                item_id,
                exc_info=True,
            )

    conn.commit()
    already_known = len(items) - stored - skipped_no_id - failed
    logger.info(
        "source=%s: %d new item(s) stored, %d already known (untouched), "
        "%d skipped (no id), %d failed",
        reading.source,
        stored,
        already_known,
        skipped_no_id,
        failed,
    )
    return stored
