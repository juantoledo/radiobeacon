import dataclasses
import json
import logging
import os
import sqlite3
import threading
from datetime import datetime, timedelta
from enum import Enum
from pathlib import Path
from string import Template
from typing import Any, Callable

from adapters.beacon_defaults import BEACON_TYPE_DEFAULT

logger = logging.getLogger(__name__)

REPO_ROOT = Path(__file__).resolve().parents[3]
DEFAULT_DB_PATH = REPO_ROOT / "storage" / "radiobeacon.db"

_audit_event_hooks: list[Callable[..., None]] = []

_BOOTSTRAP_LOCK = threading.Lock()
_BOOTSTRAPPED: set[tuple] = set()   # {(kind, st_dev, st_ino)}


def _db_identity(conn: sqlite3.Connection) -> tuple[int, int] | None:
    """Stable identity of the real database file behind `conn`, or None
    when there isn't one to identify (":memory:"/temp DBs report an empty
    filename via PRAGMA database_list — never cache those; two connections
    opened with the identical ":memory:" path string are two genuinely
    separate, independently-empty databases). Keyed on (st_dev, st_ino)
    rather than the path string so a file deleted and recreated under the
    same path (a different database) still re-bootstraps."""
    try:
        filename = next(
            (r[2] for r in conn.execute("PRAGMA database_list") if r[1] == "main"), ""
        )
    except sqlite3.Error:
        return None
    if not filename:
        return None
    try:
        st = os.stat(filename)
    except OSError:
        return None
    return (st.st_dev, st.st_ino)


def _ensure_bootstrapped(conn: sqlite3.Connection, kind: str, bootstrap: Callable[[sqlite3.Connection], None]) -> None:
    """Runs bootstrap(conn) at most once per process per real database
    file. The lock guards only the set membership, never the SQL itself —
    two threads racing the very first bootstrap of a fresh file both run
    it, which is today's existing behavior and already self-healing
    (idempotent CREATE TABLE/INDEX IF NOT EXISTS); holding a lock across
    ~20 statements would serialize every thread's first connection open
    for no correctness gain."""
    identity = _db_identity(conn)
    if identity is None:
        bootstrap(conn)   # :memory:/temp: never cached, always fresh
        return
    key = (kind, *identity)
    with _BOOTSTRAP_LOCK:
        if key in _BOOTSTRAPPED:
            return
    bootstrap(conn)
    with _BOOTSTRAP_LOCK:
        _BOOTSTRAPPED.add(key)


def reset_schema_bootstrap_cache() -> None:
    """Test hook — call from an autouse fixture so inode reuse across
    different tests' temp dirs within one pytest process can't produce a
    false cache hit."""
    with _BOOTSTRAP_LOCK:
        _BOOTSTRAPPED.clear()


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
    policy TEXT,
    source_date_time TEXT,
    fetched_at TEXT NOT NULL,
    captured_at TEXT NOT NULL DEFAULT (datetime('now')),
    rawdata TEXT NOT NULL,
    PRIMARY KEY (source, item_id)
);
"""

_CREATE_ITEMS_CAPTURED_AT_INDEX = (
    "CREATE INDEX IF NOT EXISTS idx_items_captured_at ON items (captured_at);"
)
_CREATE_ITEMS_SOURCE_DATE_TIME_INDEX = (
    "CREATE INDEX IF NOT EXISTS idx_items_source_date_time ON items (source_date_time DESC);"
)

# audit_log is the one durable, queryable record of pipeline events —
# unlike stdlib logging (stdout only, not persisted). Every package writes
# to it exclusively through record_audit_event() below, never directly, so
# the column set/contract stays uniform regardless of which package or
# event produced a row. `source`/`item_id` are nullable: some events (e.g.
# a policies edit) aren't about any one item.
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
_CREATE_AUDIT_LOG_RECORDED_AT_INDEX = (
    "CREATE INDEX IF NOT EXISTS idx_audit_log_recorded_at ON audit_log (recorded_at);"
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
# as policies' `name` PK. `is_secret` drives UI masking and stops
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

# Login accounts for ui/ — role is a fixed two-value enum (admin/user), not
# its own lookup table, since nothing in this app needs a growing set of
# roles. No declared FOREIGN KEY out of sessions.user_id (nothing in this
# schema declares one — PRAGMA foreign_keys is never turned on repo-wide);
# cascade-on-delete is explicit in adapters.auth.delete_user instead.
_CREATE_USERS = """
CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    username TEXT NOT NULL UNIQUE,
    password_hash TEXT NOT NULL,
    role TEXT NOT NULL CHECK (role IN ('admin', 'user')),
    disabled INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);
"""

# token_hash stores sha256(raw session cookie value), never the raw token
# itself — so a DB read (a backup, or even the admin-gated /dev/sql
# read-only runner) never yields a directly replayable session.
_CREATE_SESSIONS = """
CREATE TABLE IF NOT EXISTS sessions (
    token_hash TEXT PRIMARY KEY,
    user_id INTEGER NOT NULL,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    last_seen_at TEXT NOT NULL DEFAULT (datetime('now')),
    expires_at TEXT NOT NULL
);
"""
_CREATE_SESSIONS_USER_ID_INDEX = (
    "CREATE INDEX IF NOT EXISTS idx_sessions_user_id ON sessions (user_id)"
)
_CREATE_SESSIONS_EXPIRES_AT_INDEX = (
    "CREATE INDEX IF NOT EXISTS idx_sessions_expires_at ON sessions (expires_at)"
)

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
# beacon. Same shape as the policies table (also owned by this
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
# those modules). `policy` names a row in `policies` (adapters.policy) — the
# single definition of how this instance's items behave (fetch cadence,
# process, transmit). NULL resolves to DEFAULT_POLICY_NAME. Read/written by
# adapters.__main__'s DB-driven loader and by ui's /adapters CRUD pages.
_CREATE_ADAPTER_INSTANCES = """
CREATE TABLE IF NOT EXISTS adapter_instances (
    source TEXT PRIMARY KEY,
    adapter_type TEXT NOT NULL,
    enabled INTEGER NOT NULL DEFAULT 1,
    policy TEXT,
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
# template) and `ai_on_failure` ("continue_with_contents" default /
# "use_title" / "abort" — what to do when there's no real AI summary; see
# actions.ai._resolve_ai_on_failure; seeded "use_title" for senapred, absent
# elsewhere). So unlike the old senapred module,
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
                # adapters.categories curated keys: SENAPRED's own
                # "Alerta"/"Evento" split maps directly onto emergency's
                # alert/event subtypes. variableRiesgo.nombre (the specific
                # risk domain, e.g. "Hidrometeorologico") isn't part of the
                # curated set and stays available via `raw` instead of
                # being forced into subtype.
                "type": "emergency",
                "subtype": "alert" if item_type == "Alerta" else "event",
                "source_date_time": to_utc(datetime.fromisoformat(item["fechaHora"])),
                "raw": item,
            }
        )
    return items
'''


# Per-adapter override of the global ACTIONS_AI_PROMPT (see
# adapters.actions_defaults.AI_PROMPT_DEFAULT) — SENAPRED reports are
# official emergency bulletins, so the default summarization prompt is
# tuned to keep exact physical measurements (never inventing a
# rounder-sounding number in their place), strip anything about people,
# spell out earthquake date/time/intensity, and flag the report's status
# (preliminary / update N / closure). Overridable via
# ADAPTERS_SENAPRED_AI_PROMPT, same as this seed's other ADAPTERS_SENAPRED_*
# knobs; only takes effect on a fresh install (see
# _ensure_adapter_instances_seeded) — an existing adapter_instances.senapred
# row keeps whatever ai_prompt it already has.
_SENAPRED_AI_PROMPT_DEFAULT = (
    "Resume el siguiente reporte de SENAPRED en un máximo de 3 oraciones, "
    "siguiendo estas reglas:\n\n"
    "0. CONTEXTO TEMPORAL: El reporte fue emitido por SENAPRED el "
    "{source_date_time}. Usa este dato como referencia para interpretar "
    "correctamente expresiones de tiempo relativas del texto original (ej. "
    "\"esta mañana\", \"en las últimas horas\", \"hoy\"). No es necesario "
    "mencionar esta fecha en el resumen, salvo que sea la única fecha/hora "
    "disponible para un evento sísmico (ver regla 3).\n\n"
    "1. PERSONAS: No menciones nada relacionado con personas (fallecidos, "
    "heridos, evacuados, damnificados, albergados, aislados, lesionados, "
    "etc.), ni en cifras ni en palabras. Omite por completo esa información, "
    "aunque el reporte la incluya.\n\n"
    "2. UNIDADES DE MEDIDA: Cuando menciones variables físicas (temperatura, "
    "precipitación, viento, nieve, altura de nieve, etc.), SÍ debes incluir "
    "el valor numérico exacto junto con su unidad, tal como aparece en el "
    "reporte (ej. \"temperaturas de -5°C\", \"precipitaciones de 20 mm\", "
    "\"vientos de 60 km/h\"). No las conviertas a formato cualitativo.\n\n"
    "3. SISMOS: Si el reporte corresponde a un sismo, incluye fecha y hora "
    "de ocurrencia, y la intensidad Mercalli. Si la intensidad viene en "
    "números romanos, conviértela a números arábigos (ej. \"VII\" → \"7\").\n"
    "   - Formato de fecha: escribe la fecha en palabras, no en formato "
    "numérico (ej. \"3 de septiembre de 2026\", no \"03-09-2026\" ni "
    "\"03/09/2026\").\n"
    "   - Formato de hora: usa formato natural (ej. \"a las 14:32 horas\"), "
    "evitando notación abreviada tipo \"14:32:00\".\n"
    "   - Si el reporte no especifica la fecha/hora exacta del sismo, usa "
    "{source_date_time} como referencia.\n\n"
    "4. ESTADO DEL REPORTE: Revisa el título y el contenido para identificar "
    "si el reporte se describe como \"preliminar\", \"actualización\" "
    "(indicando el número si corresponde, ej. \"actualización N°3\"), "
    "\"informe de cierre\", \"última hora\", o similar. Si encuentras esta "
    "información, inclúyela explícitamente en el resumen (ej. \"Según "
    "información preliminar...\", \"En su tercera actualización, SENAPRED "
    "informó...\"). Si no se especifica nada al respecto, no lo menciones ni "
    "lo infieras.\n\n"
    "5. CONTENIDO: Céntrate solo en los hechos principales: qué evento "
    "ocurrió, qué región/comunas fueron afectadas, qué tipo de daños o "
    "condiciones se registraron, el estado de las rutas (si aplica), el "
    "retorno gradual de establecimientos educacionales (si aplica), y las "
    "alertas vigentes (tipo de alerta, zona y motivo).\n\n"
    "6. FORMATO Y TONO: Redacta en prosa corrida, sin listas, tablas ni "
    "markdown. Usa un tono informativo y neutro, tipo titular de noticia.\n\n"
    "Texto a resumir: {extracted_contents}\n"
    "Título: {extracted_title}\n"
    "Fecha y hora de emisión del reporte: {source_date_time}"
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
                # adapters.categories curated keys — see that module's
                # docstring; "seismology"/"quake" is what gets this item a
                # category icon wherever items are displayed.
                "type": {"template": "seismology"},
                "subtype": {"template": "quake"},
            },
            "date_field": "Fecha",
            "date_format": "%Y-%m-%d %H:%M:%S",
            "source_timezone": get("ADAPTERS_CSN_SOURCE_TZ", "America/Santiago"),
        },
        "default",
    ),
    (
        "senapred",
        "custom",
        lambda get: {
            # Emergency alerts: keep the item on air even when the AI
            # summarizer's provider call fails — actions.ai then stores the
            # title as the summary instead of letting the item hard-stop.
            "ai_on_failure": "use_title",
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
        "default",
    ),
)


# `policies` is the single definition of how an item behaves end to end —
# fetch cadence, process (event-driven, no tunable yet), and transmit
# (how many times / how far apart / on a cron). items.policy and
# adapter_instances.policy reference a row here by name. Owned by this
# module (not beacon/ or dispatcher/) so every package reaches it through
# the same get_connection() as every other table. See adapters.policy.
_CREATE_POLICIES = """
CREATE TABLE IF NOT EXISTS policies (
    name                      TEXT PRIMARY KEY,
    fetch_kind                TEXT    NOT NULL DEFAULT 'interval',
    fetch_interval_seconds    INTEGER,
    fetch_cron                TEXT,
    process_mode              TEXT    NOT NULL DEFAULT 'on_new_data',
    transmit_kind             TEXT    NOT NULL DEFAULT 'once',
    transmit_count            INTEGER NOT NULL DEFAULT 1,
    transmit_interval_seconds INTEGER NOT NULL DEFAULT 0,
    transmit_cron             TEXT,
    description               TEXT
);
"""

# beacon_tx_schedule is the durable source of truth for what beacon/ still
# has to put on air and how many more times — it replaced beacon's
# in-memory drop-oldest queues, so a beacon restart no longer loses pending
# transmissions. One row per unit of transmittable content: `kind` is the
# beacon type it belongs to ("frame" | "voice"), matching BEACON_TYPE — only
# one kind is ever scheduled/drained at a time. `ref` distinguishes units
# within a kind for one item (frame = str(chunk_index), voice = "").
# `policy` is a NAME snapshot; the actual transmit schedule
# (kind/count/interval_seconds/cron) is resolved live every cycle via
# adapters.policy.policy_for, so editing a Policy stays reactive.
# `sent_count` is stored progress; a row is deleted once it reaches the
# policy's transmit_count. A rearm / policy change re-inserts the row
# (PK upsert) resetting sent_count and last_transmitted_at.
_CREATE_BEACON_TX_SCHEDULE = """
CREATE TABLE IF NOT EXISTS beacon_tx_schedule (
    source              TEXT NOT NULL,
    item_id             TEXT NOT NULL,
    kind                TEXT NOT NULL,
    ref                 TEXT NOT NULL DEFAULT '',
    policy              TEXT,
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

# beacon_manual_tx holds one-shot messages an operator types into the
# dashboard's "Transmit now" action (ui.routers.manual_tx) for beacon/ to
# put on air on its next tick (beacon.__main__._drain_manual_tx). Unlike
# beacon_tx_schedule these carry their own literal `text` (there's no item
# to resolve it from), have no policy (sent exactly once), and are
# deleted the moment they've been attempted — success or failure. `kind`
# ("voice" | "frame") is the beacon type the message was composed for; the
# beacon only drains rows matching the currently-active BEACON_TYPE, so a
# row for the other kind waits until the operator switches mode.
_CREATE_BEACON_MANUAL_TX = """
CREATE TABLE IF NOT EXISTS beacon_manual_tx (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    kind       TEXT NOT NULL,
    text       TEXT NOT NULL,
    actor      TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
"""


# (old_column, new_column): renames applied in order to databases created
# before a given schema change.
_COLUMN_RENAMES = (
    ("data", "rawdata"),
    ("title", "extracted_title"),
    ("contents", "extracted_contents"),
    ("url_access", "event_key"),
)
_NEW_TEXT_COLUMNS = (
    "extracted_title",
    "extracted_contents",
    "summary",
    "url",
    "event_key",
    "type",
    "subtype",
    "policy",
    "source_date_time",
)
# Columns from an older schema — dropped on migration if present.
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


def _ensure_items_indexes(conn: sqlite3.Connection) -> None:
    """Call only after _migrate_items_table — source_date_time is added by
    that migration, and captured_at is not in _NEW_TEXT_COLUMNS at all, so
    a pre-captured_at legacy database genuinely lacks that column. A
    missing column must stay a harmless no-op, never a hard failure that
    stops get_connection from returning."""
    for statement in (_CREATE_ITEMS_CAPTURED_AT_INDEX, _CREATE_ITEMS_SOURCE_DATE_TIME_INDEX):
        try:
            conn.execute(statement)
        except sqlite3.OperationalError:
            logger.debug("skipped items index (column not present yet): %s", statement)


def _ensure_policies_table(conn: sqlite3.Connection) -> None:
    """Idempotent, and safe to call on any connection — same pattern as
    _ensure_sources_table."""
    conn.execute(_CREATE_POLICIES)


def _ensure_policies_seeded(conn: sqlite3.Connection) -> None:
    """Inserts the seed Policy (`default`) only if the table is empty —
    mirrors _ensure_sources_seeded. An operator's later edits (or
    deletions) via dispatcher/policies.sh or the ui's /config/policies page
    are never overwritten on a subsequent get_connection() call."""
    _ensure_policies_table(conn)
    count = conn.execute("SELECT COUNT(*) FROM policies").fetchone()[0]
    if count == 0:
        from adapters.policy import SEED_POLICIES

        conn.executemany(
            "INSERT INTO policies "
            "(name, fetch_kind, fetch_interval_seconds, fetch_cron, process_mode, "
            "transmit_kind, transmit_count, transmit_interval_seconds, transmit_cron, description) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            SEED_POLICIES,
        )
        conn.commit()


def _ensure_beacon_tx_schedule_table(conn: sqlite3.Connection) -> None:
    """Idempotent, and safe to call on any connection — the beacon TX
    helpers below call this themselves, same pattern as
    _ensure_audit_log_table. Routes through the process-wide bootstrap
    cache (kind="beacon_tx_schedule") since these helpers are called on
    every beacon TX loop tick, independent of get_connection."""
    def _create(c: sqlite3.Connection) -> None:
        c.execute(_CREATE_BEACON_TX_SCHEDULE)
        c.execute(_CREATE_BEACON_TX_SCHEDULE_KIND_INDEX)

    _ensure_bootstrapped(conn, "beacon_tx_schedule", _create)


def _ensure_beacon_manual_tx_table(conn: sqlite3.Connection) -> None:
    """Idempotent, and safe to call on any connection — the manual-tx
    helpers below call this themselves, same pattern as
    _ensure_beacon_tx_schedule_table. Routes through the process-wide
    bootstrap cache (kind="beacon_manual_tx")."""
    _ensure_bootstrapped(conn, "beacon_manual_tx", lambda c: c.execute(_CREATE_BEACON_MANUAL_TX))


# One row per kind ("logo" today) — an admin-uploaded, server-normalized
# image (see ui.branding.save_logo), unlike every other admin-supplied
# asset in this repo (e.g. beacon TTS WAV clips), which lives on disk with
# only a filename in the DB. Deliberately a DB blob instead: there's only
# ever one row per kind, so SQLite's usual "don't put large binaries in
# the hot WAL path" concern doesn't apply the way it would for
# high-volume per-item data, and it means a DB backup captures branding
# automatically along with everything else.
_CREATE_BRAND_ASSETS = """
CREATE TABLE IF NOT EXISTS brand_assets (
    kind         TEXT PRIMARY KEY,
    content      BLOB NOT NULL,
    content_type TEXT NOT NULL,
    width        INTEGER NOT NULL,
    height       INTEGER NOT NULL,
    checksum     TEXT NOT NULL,
    updated_at   TEXT NOT NULL DEFAULT (datetime('now')),
    updated_by   TEXT NOT NULL
);
"""


def _ensure_brand_assets_table(conn: sqlite3.Connection) -> None:
    """Idempotent, and safe to call on any connection — the brand-asset
    helpers below call this themselves, same pattern as
    _ensure_beacon_manual_tx_table. Routes through the process-wide
    bootstrap cache (kind="brand_assets")."""
    _ensure_bootstrapped(conn, "brand_assets", lambda c: c.execute(_CREATE_BRAND_ASSETS))


def get_brand_asset(conn: sqlite3.Connection, kind: str) -> dict[str, Any] | None:
    """The stored asset row for `kind` (e.g. "logo"), or None if unset.
    `checksum` is what ui.templating's logo_url() uses to cache-bust the
    image URL on re-upload, the same role a static file's mtime plays for
    static_url()."""
    _ensure_brand_assets_table(conn)
    row = conn.execute(
        "SELECT content, content_type, width, height, checksum, updated_at "
        "FROM brand_assets WHERE kind = ?",
        (kind,),
    ).fetchone()
    if row is None:
        return None
    content, content_type, width, height, checksum, updated_at = row
    return {
        "content": content,
        "content_type": content_type,
        "width": width,
        "height": height,
        "checksum": checksum,
        "updated_at": updated_at,
    }


def set_brand_asset(
    conn: sqlite3.Connection,
    kind: str,
    *,
    content: bytes,
    content_type: str,
    width: int,
    height: int,
    checksum: str,
    actor: str,
) -> None:
    """Upserts one brand asset row and records a branding.<kind>_uploaded
    audit event — same actor-attributed audit trail every other admin
    write in this app gets. `content` is the already-validated,
    already-normalized image (see ui.branding.save_logo); this function
    does no validation of its own."""
    _ensure_brand_assets_table(conn)
    conn.execute(
        "INSERT INTO brand_assets "
        "(kind, content, content_type, width, height, checksum, updated_at, updated_by) "
        "VALUES (?, ?, ?, ?, ?, ?, datetime('now'), ?) "
        "ON CONFLICT(kind) DO UPDATE SET "
        "content = excluded.content, content_type = excluded.content_type, "
        "width = excluded.width, height = excluded.height, "
        "checksum = excluded.checksum, updated_at = excluded.updated_at, "
        "updated_by = excluded.updated_by",
        (kind, content, content_type, width, height, checksum, actor),
    )
    conn.commit()
    record_audit_event(
        conn,
        event_type=f"branding.{kind}_uploaded",
        actor=actor,
        details={"content_type": content_type, "width": width, "height": height},
    )


def delete_brand_asset(conn: sqlite3.Connection, kind: str, *, actor: str) -> bool:
    """Removes the stored asset for `kind`, if any. Returns whether a row
    was actually deleted (so the caller can skip the audit event / toast
    for a no-op remove)."""
    _ensure_brand_assets_table(conn)
    cur = conn.execute("DELETE FROM brand_assets WHERE kind = ?", (kind,))
    conn.commit()
    removed = cur.rowcount > 0
    if removed:
        record_audit_event(conn, event_type=f"branding.{kind}_removed", actor=actor)
    return removed


def _backfill_senapred_ai_on_failure(conn: sqlite3.Connection) -> None:
    """One-time, best-effort backfill: senapred is seeded with
    `ai_on_failure="use_title"` (see _SEED_ADAPTER_INSTANCES), but the seed
    only runs on an empty table, so a database created before that key
    existed keeps senapred without it (possibly with the old boolean
    `ai_fallback_to_title`). Set `ai_on_failure="use_title"` and drop the
    legacy key only when the senapred row exists and has no `ai_on_failure`
    yet — an operator who set it explicitly is left untouched, and no other
    source is touched. Locked-DB safe, never blocks startup."""
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
    if not isinstance(cfg, dict) or "ai_on_failure" in cfg:
        return
    cfg.pop("ai_fallback_to_title", None)
    cfg["ai_on_failure"] = "use_title"
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
            details={"migrate": "ai_fallback_to_title -> ai_on_failure=use_title"},
        )
    except sqlite3.OperationalError:
        logger.debug("adapter_instances.senapred ai_on_failure backfill skipped (locked)")


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
        _ensure_bootstrapped(conn, "schema", _bootstrap_schema)
        # The three _ensure_*_seeded calls stay outside the cached
        # bootstrap and run on every single get_connection call, unlike
        # everything above — each is just one cheap `SELECT COUNT(*)`
        # (its own table's already-cached _ensure_*_table call makes even
        # that redundant CREATE TABLE a no-op), and "insert seed rows only
        # when the table is empty" must keep working even after this
        # process's very first bootstrap of this database file: e.g. an
        # operator (or a test) that deletes every adapter_instances row
        # expects the very next get_connection() to reseed it, not to wait
        # for a process restart. Skipping these along with the rest of
        # _bootstrap_schema would silently break that.
        _ensure_sources_seeded(conn)
        _ensure_policies_seeded(conn)
        _ensure_adapter_instances_seeded(conn)
        conn.commit()
    except Exception:
        conn.close()
        raise
    logger.debug("opened sqlite connection at %s", path)
    return conn


def _bootstrap_schema(conn: sqlite3.Connection) -> None:
    """The one-time-per-database-file bootstrap sequence get_connection
    used to re-run in full on every call — schema creation, migrations,
    every _ensure_*_table, and the senapred backfill. Extracted so it can
    be run through _ensure_bootstrapped and cached on the real database
    file's identity (see _db_identity/_ensure_bootstrapped above).

    Deliberately excludes _ensure_sources_seeded/_ensure_policies_seeded/
    _ensure_adapter_instances_seeded — see get_connection's comment for
    why those three stay uncached and run on every call instead."""
    conn.executescript(SCHEMA)
    _migrate_items_table(conn)
    _ensure_items_indexes(conn)
    _ensure_audit_log_table(conn)
    _ensure_chunks_table(conn)
    _ensure_settings_table(conn)
    _ensure_users_table(conn)
    _ensure_sessions_table(conn)
    _ensure_beacon_status_table(conn)
    _ensure_item_readiness_table(conn)
    _ensure_sources_table(conn)
    _ensure_policies_table(conn)
    _ensure_adapter_instances_table(conn)
    _ensure_beacon_tx_schedule_table(conn)
    _ensure_beacon_manual_tx_table(conn)
    _ensure_brand_assets_table(conn)
    _backfill_senapred_ai_on_failure(conn)


def _ensure_audit_log_table(conn: sqlite3.Connection) -> None:
    """Idempotent, and safe to call on any connection (not just ones from
    get_connection) — record_audit_event calls this itself, so a caller
    that hand-rolls a bare sqlite3 connection (e.g. a test) doesn't need to
    separately know about this table. Routes through the process-wide
    bootstrap cache (kind="audit_log") since record_audit_event is on the
    hottest path in the repo (every MQTT message, ~110 call sites)."""
    def _create(c: sqlite3.Connection) -> None:
        c.execute(_CREATE_AUDIT_LOG)
        c.execute(_CREATE_AUDIT_LOG_SOURCE_ITEM_INDEX)
        c.execute(_CREATE_AUDIT_LOG_EVENT_TYPE_INDEX)
        c.execute(_CREATE_AUDIT_LOG_RECORDED_AT_INDEX)

    _ensure_bootstrapped(conn, "audit_log", _create)


def dispatch_audit_event_hooks(*payloads: dict) -> None:
    """Fires every registered audit-event hook for each payload, in order.
    Each payload is the same keyword-argument shape record_audit_event
    passes to a hook (event_type, actor, source, item_id, details). A hook
    that raises is logged and swallowed, exactly like record_audit_event's
    own inline dispatch — used both by record_audit_event(commit=True)
    itself and by a caller (e.g. store_reading) batching several rows
    under one commit and firing all of their hooks afterward."""
    for payload in payloads:
        for hook in _audit_event_hooks:
            try:
                hook(**payload)
            except Exception:
                logger.error("audit event hook failed hook=%r", hook, exc_info=True)


def record_audit_event(
    conn: sqlite3.Connection,
    *,
    event_type: str,
    actor: str,
    source: str | None = None,
    item_id: str | None = None,
    details: dict[str, Any] | None = None,
    commit: bool = True,
) -> dict[str, Any] | None:
    """The one contract every package writes audit rows through — same
    function, same column set, regardless of which package or which event
    produced it. `actor` identifies what wrote the row (e.g.
    "adapters.CustomAdapter", "dispatcher.watcher"). `details` is optional free-form JSON (reuses
    _json_default for datetime/dataclass/Enum values, same as rawdata).

    commit=True (the default, unchanged behavior for every existing call
    site): commits immediately and fires every hook registered via
    register_audit_event_hook() immediately after — see that function for
    the contract (best-effort, never raises). Returns None.

    commit=False: writes the INSERT into the caller's already-open
    transaction and returns the hook payload dict instead of firing hooks
    — the row is NOT committed and hooks are NOT fired. For a caller
    batching several audit rows under one transaction (e.g. store_reading)
    that wants exactly one commit for the whole batch, then to fire every
    collected payload's hooks afterward via dispatch_audit_event_hooks —
    so a hook publishing to MQTT still never fires for an uncommitted
    row."""
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
    payload = {
        "event_type": event_type,
        "actor": actor,
        "source": source,
        "item_id": item_id,
        "details": details,
    }
    if not commit:
        return payload

    conn.commit()
    dispatch_audit_event_hooks(payload)
    return None


def _ensure_chunks_table(conn: sqlite3.Connection) -> None:
    """Idempotent, and safe to call on any connection — store_chunks calls
    this itself, same pattern as _ensure_audit_log_table. Routes through
    the process-wide bootstrap cache (kind="chunks")."""
    _ensure_bootstrapped(conn, "chunks", lambda c: c.execute(_CREATE_CHUNKS))


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


def item_exists(conn: sqlite3.Connection, source: str, item_id: str) -> bool:
    """Whether a (source, item_id) row is already stored. Used by
    adapters.aiprompt_adapter to skip an LLM call when this cron
    occurrence's item already exists (items are immutable — store_reading
    is INSERT OR IGNORE — so a second call would be wasted spend)."""
    return (
        conn.execute(
            "SELECT 1 FROM items WHERE source = ? AND item_id = ?", (source, item_id)
        ).fetchone()
        is not None
    )


def _ensure_settings_table(conn: sqlite3.Connection) -> None:
    """Idempotent, and safe to call on any connection — get_setting/
    set_setting/list_settings/delete_setting all call this themselves, same
    pattern as _ensure_audit_log_table/_ensure_chunks_table. Routes through
    the process-wide bootstrap cache (kind="settings") — get_setting is
    called on very hot paths (once per HTTP request, once per MQTT
    message) and used to independently re-check this table every time."""
    _ensure_bootstrapped(conn, "settings", lambda c: c.execute(_CREATE_SETTINGS))


def _ensure_users_table(conn: sqlite3.Connection) -> None:
    """Idempotent, and safe to call on any connection — adapters.auth's
    user functions all call this themselves, same pattern as
    _ensure_settings_table. Routes through the process-wide bootstrap cache
    (kind="users")."""
    _ensure_bootstrapped(conn, "users", lambda c: c.execute(_CREATE_USERS))


def _ensure_sessions_table(conn: sqlite3.Connection) -> None:
    """Idempotent, and safe to call on any connection — adapters.auth's
    session functions all call this themselves, same pattern as
    _ensure_settings_table. Routes through the process-wide bootstrap cache
    (kind="sessions") — session validation runs on every authenticated UI
    request."""
    def _create(c: sqlite3.Connection) -> None:
        c.execute(_CREATE_SESSIONS)
        c.execute(_CREATE_SESSIONS_USER_ID_INDEX)
        c.execute(_CREATE_SESSIONS_EXPIRES_AT_INDEX)

    _ensure_bootstrapped(conn, "sessions", _create)


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


def get_settings(keys, *, conn: sqlite3.Connection, env_fallback: bool = True) -> dict:
    """Bulk get_setting: one SELECT instead of one query per key. `keys` is
    a mapping of key -> default. Applies the identical DB row -> env var ->
    default resolution per key that get_setting uses."""
    _ensure_settings_table(conn)
    placeholders = ",".join("?" for _ in keys)
    rows = dict(
        conn.execute(f"SELECT key, value FROM settings WHERE key IN ({placeholders})", tuple(keys))
    )
    out = {}
    for key, default in keys.items():
        value = rows.get(key)
        if value is not None:
            out[key] = value
        elif env_fallback:
            out[key] = os.environ.get(key, default)
        else:
            out[key] = default
    return out


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
    pattern as _ensure_settings_table. Routes through the process-wide
    bootstrap cache (kind="beacon_status") — telemetry is written every
    beacon loop tick."""
    _ensure_bootstrapped(conn, "beacon_status", lambda c: c.execute(_CREATE_BEACON_STATUS))


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
    _ensure_settings_table. Routes through the process-wide bootstrap cache
    (kind="item_readiness")."""
    _ensure_bootstrapped(conn, "item_readiness", lambda c: c.execute(_CREATE_ITEM_READINESS))


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
    "source", "item_id", "kind", "ref", "policy",
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
    policy: str | None,
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
        "(source, item_id, kind, ref, policy, enqueued_event_id) "
        "VALUES (?, ?, ?, ?, ?, ?) "
        "ON CONFLICT (source, item_id, kind, ref) DO UPDATE SET "
        "policy = excluded.policy, "
        "enqueued_event_id = excluded.enqueued_event_id, "
        "sent_count = 0, last_transmitted_at = NULL, "
        "updated_at = datetime('now')",
        (source, item_id, kind, ref, policy, enqueued_event_id),
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
        "SELECT policy FROM items WHERE source = ? AND item_id = ?",
        (source, item_id),
    ).fetchone()
    if policy_row is None:
        return {"kind": None, "scheduled": 0}
    policy = policy_row[0]

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
                conn, source, item_id, "frame", str(chunk_index), policy, None
            )
        scheduled = len(rows)
    else:
        add_tx_schedule_unit(conn, source, item_id, "voice", "", policy, None)
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


def delete_stale_tx_schedule(
    conn: sqlite3.Connection,
    kind: str,
    *,
    max_age_seconds: int,
    now_iso: str,
) -> list[dict[str, Any]]:
    """Drops every beacon_tx_schedule row of this `kind` that has sat unsent
    longer than `max_age_seconds`, measured against `updated_at` (the last
    time the row was enqueued, rearmed, or transmitted). Returns the deleted
    rows so the caller can audit each; a no-op returning [] when
    `max_age_seconds <= 0`. `now_iso` is the caller's "now" (utc_now()),
    passed in for testability. Same policy-agnostic style as
    due_tx_schedule_rows — staleness is a wall-clock concern, unrelated to
    the Policy's transmit count/interval/cron."""
    if max_age_seconds <= 0:
        return []
    _ensure_beacon_tx_schedule_table(conn)
    # SQLite writes created_at/updated_at as `datetime('now')` -> the naive
    # UTC "%Y-%m-%d %H:%M:%S" string this cutoff must match to compare.
    cutoff = (
        datetime.fromisoformat(now_iso) - timedelta(seconds=max_age_seconds)
    ).strftime("%Y-%m-%d %H:%M:%S")
    stale = conn.execute(
        f"SELECT {', '.join(_BEACON_TX_SCHEDULE_COLUMNS)} FROM beacon_tx_schedule "
        "WHERE kind = ? AND updated_at < ? ORDER BY created_at, rowid",
        (kind, cutoff),
    ).fetchall()
    if stale:
        conn.execute(
            "DELETE FROM beacon_tx_schedule WHERE kind = ? AND updated_at < ?",
            (kind, cutoff),
        )
        conn.commit()
    return [_tx_schedule_row_to_dict(row) for row in stale]


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


# --- beacon_manual_tx: one-shot operator messages (see _CREATE_BEACON_MANUAL_TX) ---

_BEACON_MANUAL_TX_COLUMNS = ("id", "kind", "text", "actor", "created_at")


def enqueue_manual_tx(
    conn: sqlite3.Connection, *, kind: str, text: str, actor: str
) -> int:
    """Queues one operator-typed message for beacon/ to transmit once on
    its next tick. Records a beacon.manual.enqueued audit row. Returns the
    new row id. Length limits are the caller's job (ui.routers.manual_tx
    blocks an over-limit send in the browser); this only writes DB state,
    so it stays callable from ui without a TTS dependency."""
    _ensure_beacon_manual_tx_table(conn)
    cur = conn.execute(
        "INSERT INTO beacon_manual_tx (kind, text, actor) VALUES (?, ?, ?)",
        (kind, text, actor),
    )
    conn.commit()
    record_audit_event(
        conn,
        event_type="beacon.manual.enqueued",
        actor=actor,
        details={"kind": kind, "chars": len(text)},
    )
    return int(cur.lastrowid)


def pending_manual_tx(conn: sqlite3.Connection, kind: str) -> list[dict[str, Any]]:
    """Every queued manual message of this `kind`, oldest first."""
    _ensure_beacon_manual_tx_table(conn)
    rows = conn.execute(
        f"SELECT {', '.join(_BEACON_MANUAL_TX_COLUMNS)} FROM beacon_manual_tx "
        "WHERE kind = ? ORDER BY id",
        (kind,),
    ).fetchall()
    return [dict(zip(_BEACON_MANUAL_TX_COLUMNS, row)) for row in rows]


def delete_manual_tx(conn: sqlite3.Connection, id_: int) -> None:
    """Removes one manual message — beacon calls this once it's been
    attempted, whether it went on air or not (a manual send is never
    retried)."""
    _ensure_beacon_manual_tx_table(conn)
    conn.execute("DELETE FROM beacon_manual_tx WHERE id = ?", (id_,))
    conn.commit()


def count_manual_tx_by_kind(conn: sqlite3.Connection) -> dict[str, int]:
    """{kind: queued-message-count} for the dashboard."""
    _ensure_beacon_manual_tx_table(conn)
    return {
        kind: count
        for kind, count in conn.execute(
            "SELECT kind, COUNT(*) FROM beacon_manual_tx GROUP BY kind"
        )
    }


def _ensure_sources_table(conn: sqlite3.Connection) -> None:
    """Idempotent, and safe to call on any connection — same pattern as
    _ensure_settings_table/_ensure_item_readiness_table. Routes through
    the process-wide bootstrap cache (kind="sources") — get_source_fields
    calls this on every item render."""
    _ensure_bootstrapped(conn, "sources", lambda c: c.execute(_CREATE_SOURCES))


def _ensure_sources_seeded(conn: sqlite3.Connection) -> None:
    """Inserts _SEED_SOURCES only if the table is empty — mirrors
    _ensure_policies_seeded exactly, so a fresh database starts
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
    Mirrors adapters.policy.set_policy's upsert-then-audit shape."""
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
    Mirrors adapters.policy.delete_policy."""
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
    _ensure_sources_table. Routes through the process-wide bootstrap cache
    (kind="adapter_instances") — get_adapter_instance is on the item
    rendering hot path (beacon.content, actions)."""
    _ensure_bootstrapped(conn, "adapter_instances", lambda c: c.execute(_CREATE_ADAPTER_INSTANCES))


def _ensure_adapter_instances_seeded(conn: sqlite3.Connection) -> None:
    """Inserts a csn (api) and senapred (custom) row only if the table is
    empty — mirrors _ensure_sources_seeded exactly. Each seed's config is
    built by calling its lambda with a `get(key, default)` helper reading
    adapters.storage.get_setting(key, default, conn=conn) — so if this
    operator's live DB already had any of the legacy ADAPTERS_CSN_*/
    ADAPTERS_SENAPRED_* settings rows (set via the old /config groups),
    those values carry into the seeded row's JSON config instead of
    silently reverting to the hardcoded default. Both seeded rows point at
    the seeded `default` Policy."""
    _ensure_adapter_instances_table(conn)
    count = conn.execute("SELECT COUNT(*) FROM adapter_instances").fetchone()[0]
    if count != 0:
        return

    def get(key: str, default: str) -> str:
        return get_setting(key, default, conn=conn) or default

    for source, adapter_type, config_builder, policy in _SEED_ADAPTER_INSTANCES:
        conn.execute(
            "INSERT INTO adapter_instances "
            "(source, adapter_type, enabled, policy, config) "
            "VALUES (?, ?, 1, ?, ?)",
            (
                source,
                adapter_type,
                policy,
                json.dumps(config_builder(get)),
            ),
        )
    conn.commit()


_ADAPTER_INSTANCE_COLUMNS = (
    "source", "adapter_type", "enabled", "policy", "config", "updated_at", "updated_by",
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
    policy: str | None = None,
    actor: str = "ui.adapters",
) -> None:
    """Creates or replaces one adapter_instances row. Mirrors
    set_source's upsert-then-audit shape. `config` is passed as a dict and
    serialized here (not by the caller) so every writer produces
    consistently-formatted JSON. `policy` names a row in `policies`
    (NULL resolves to DEFAULT_POLICY_NAME)."""
    _ensure_adapter_instances_table(conn)
    conn.execute(
        "INSERT INTO adapter_instances "
        "(source, adapter_type, enabled, policy, config, updated_at, updated_by) "
        "VALUES (?, ?, ?, ?, ?, datetime('now'), ?) "
        "ON CONFLICT(source) DO UPDATE SET "
        "adapter_type = excluded.adapter_type, "
        "enabled = excluded.enabled, "
        "policy = excluded.policy, "
        "config = excluded.config, "
        "updated_at = excluded.updated_at, "
        "updated_by = excluded.updated_by",
        (source, adapter_type, int(enabled), policy, json.dumps(config), actor),
    )
    conn.commit()
    record_audit_event(
        conn,
        event_type="adapter_instance.set",
        actor=actor,
        source=source,
        details={"adapter_type": adapter_type, "enabled": enabled, "policy": policy},
    )


def set_adapter_instance_enabled(
    conn: sqlite3.Connection, source: str, enabled: bool, *, actor: str = "ui.adapters"
) -> bool:
    """Flips only the enabled flag, leaving adapter_type/policy/config untouched.
    Returns whether a row was actually updated. Mirrors delete_adapter_instance's
    upsert-then-audit shape."""
    _ensure_adapter_instances_table(conn)
    cursor = conn.execute(
        "UPDATE adapter_instances SET enabled = ?, updated_at = datetime('now'), updated_by = ? "
        "WHERE source = ?",
        (int(enabled), actor, source),
    )
    conn.commit()
    if cursor.rowcount > 0:
        record_audit_event(
            conn,
            event_type="adapter_instance.enabled" if enabled else "adapter_instance.disabled",
            actor=actor,
            source=source,
            details={"enabled": enabled},
        )
    return cursor.rowcount > 0


def delete_adapter_instance(conn: sqlite3.Connection, source: str, *, actor: str = "ui.adapters") -> bool:
    """Returns whether a row was actually deleted. Mirrors delete_source —
    leaves historical `items` rows and the `sources` display-metadata row
    untouched, same as deleting a Policy doesn't delete items."""
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
    `type` is "emergency" and `subtype` is "alert"/"event" (which of its
    two separate GraphQL feeds an item came from). See adapters.categories
    for the curated set of type/subtype keys an adapter can use to get a
    category icon in the UI; any adapter with a similar broad/fine category
    split can use the same two columns, curated or not.

    `policy` names the Policy this item behaves under — a soft reference
    (not a SQL FOREIGN KEY) to `policies.name` (see adapters.policy). It is
    set to whatever the item itself carried (a custom snippet may set one
    per item), else the adapter instance's own `policy`
    (adapter_instances.policy), else left NULL — resolving to
    DEFAULT_POLICY_NAME at read time. It can be re-pointed to a different
    whole Policy afterward by a human/UI (dispatcher/override_item.py) —
    unlike the rest of the row, not immutable-forever, only
    adapter-untouched-after-insert.

    `summary`, like `policy`, is never touched here beyond the initial
    insert — a separate actor sets `summary` afterward; it starts NULL.

    Returns the number of newly stored (previously unseen) items."""
    stored = 0
    skipped_no_id = 0
    failed = 0
    audit_payloads = []
    items = reading.data if isinstance(reading.data, list) else []
    instance_policy = conn.execute(
        "SELECT policy FROM adapter_instances WHERE source = ?", (reading.source,)
    ).fetchone()
    instance_policy = instance_policy[0] if instance_policy is not None else None
    for item in items:
        item_id = getattr(item, "id", None)
        if item_id is None:
            skipped_no_id += 1
            continue
        try:
            cursor = conn.execute(
                "INSERT OR IGNORE INTO items "
                "(source, item_id, extracted_title, extracted_contents, "
                "summary, url, event_key, type, subtype, policy, "
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
                    _as_text(getattr(item, "policy", None)) or instance_policy,
                    _as_text(getattr(item, "source_date_time", None)),
                    reading.fetched_at.isoformat(),
                    json.dumps(item, default=_json_default),
                ),
            )
            if cursor.rowcount:
                payload = record_audit_event(
                    conn,
                    event_type="item.stored",
                    actor="adapters.storage",
                    source=reading.source,
                    item_id=item_id,
                    commit=False,
                )
                audit_payloads.append(payload)
            stored += cursor.rowcount
        except Exception:
            # One malformed/unexpected item must never discard every item
            # processed earlier in this same batch — those are still
            # pending in this transaction and get committed below along
            # with everything else; only this one item is skipped.
            failed += 1
            logger.error(
                "failed to store item, skipping source=%s item_id=%s",
                reading.source,
                item_id,
                exc_info=True,
            )

    conn.commit()
    dispatch_audit_event_hooks(*audit_payloads)
    already_known = len(items) - stored - skipped_no_id - failed
    logger.info(
        "store done source=%s new=%d already_known=%d skipped_no_id=%d failed=%d",
        reading.source,
        stored,
        already_known,
        skipped_no_id,
        failed,
    )
    return stored
