"""The Policy object — the single definition of how an item behaves across
the whole pipeline: how often it is **fetched**, how it is **processed**
(event-driven, no tunable yet), and how it is **transmitted** (how many
times, how far apart, or on a cron).

A Policy is a named row in the `policies` table (adapters.storage). An
adapter points at exactly one Policy by name (`adapter_instances.policy`);
every item it produces carries that name (`items.policy`). Nothing patches
a Policy partially — different behaviour means a different named Policy.

Resolved fresh on every `resolve_policy` / `policy_for` call, so editing a
Policy's numbers (or an item's `policy`) takes effect on the next lookup —
the runner re-resolves each loop iteration, beacon re-resolves each drain
cycle.
"""
import sqlite3
from dataclasses import dataclass
from datetime import datetime

from adapters.storage import get_setting, record_audit_event

DEFAULT_POLICY_NAME = "default"

# Fetch fallback when a Policy row has fetch_kind='interval' and a NULL
# fetch_interval_seconds. Was adapters.__main__.DEFAULT_INTERVAL_SECONDS /
# the ADAPTERS_DEFAULT_INTERVAL_SECONDS setting; kept here so the whole
# fetch-cadence story lives in one module.
DEFAULT_INTERVAL_ENV_VAR = "ADAPTERS_DEFAULT_INTERVAL_SECONDS"
DEFAULT_INTERVAL_SECONDS = 10

FETCH_KINDS = ("once", "interval", "cron")
TRANSMIT_KINDS = ("once", "interval", "cron")
PROCESS_MODES = ("on_new_data",)

# Seeded into `policies` by adapters.storage._ensure_policies_seeded on the
# first get_connection() call, only if the table is empty. A fresh install
# ships just `default`; any further Policy (an `urgent` that airs 5x/60s, a
# `weather-forecast` that fetches on a cron, ...) is added by an operator
# via dispatcher/policies.sh or the ui's /config/policies page, not by
# changing this constant.
#   (name, fetch_kind, fetch_interval_seconds, fetch_cron, process_mode,
#    transmit_kind, transmit_count, transmit_interval_seconds, transmit_cron,
#    description)
SEED_POLICIES = (
    (
        "default",
        "interval", DEFAULT_INTERVAL_SECONDS, None,
        "on_new_data",
        "once", 1, 0, None,
        "Fetch every 10s, air once.",
    ),
)

_POLICY_COLUMNS = (
    "name",
    "fetch_kind", "fetch_interval_seconds", "fetch_cron",
    "process_mode",
    "transmit_kind", "transmit_count", "transmit_interval_seconds", "transmit_cron",
    "description",
)

# Absolute last-resort fallbacks if `policies` is empty or missing the
# DEFAULT_POLICY_NAME row (e.g. an operator deleted it) — the pipeline
# should never crash or loop forever from a management mistake.
_FALLBACK_FETCH_KIND = "interval"
_FALLBACK_TRANSMIT_KIND = "once"
_FALLBACK_TRANSMIT_COUNT = 1


@dataclass(frozen=True)
class Schedule:
    """One stage's schedule. `kind` is once | interval | cron.
    `interval_seconds` applies to interval, `cron` to cron, `count` caps
    airings on the transmit stage (always 1 for `once`, None on the fetch
    stage)."""

    kind: str
    interval_seconds: int = 0
    cron: str | None = None
    count: int | None = None

    def next_fire_after(self, after: datetime, *, conn: sqlite3.Connection) -> datetime | None:
        """Next occurrence strictly after `after` (a UTC-aware datetime).
        `once` -> None. `interval` -> after + interval_seconds. `cron` ->
        croniter (in DISPLAY_TIMEZONE), or None if the expression is
        invalid."""
        from datetime import timedelta

        if self.kind == "interval":
            return after + timedelta(seconds=max(1, self.interval_seconds or 0))
        if self.kind == "cron":
            from adapters.cron import next_fire_after as _cron_next

            return _cron_next(self.cron or "", after, conn=conn)
        return None


@dataclass(frozen=True)
class Policy:
    name: str
    fetch: Schedule
    transmit: Schedule


def _row_to_policy(row: tuple) -> Policy:
    d = dict(zip(_POLICY_COLUMNS, row))
    fetch_interval = d["fetch_interval_seconds"]
    if fetch_interval is None and d["fetch_kind"] == "interval":
        raw = get_setting(DEFAULT_INTERVAL_ENV_VAR)
        fetch_interval = int(raw) if raw else DEFAULT_INTERVAL_SECONDS
    return Policy(
        name=d["name"],
        fetch=Schedule(
            kind=d["fetch_kind"],
            interval_seconds=fetch_interval or 0,
            cron=d["fetch_cron"],
        ),
        transmit=Schedule(
            kind=d["transmit_kind"],
            interval_seconds=d["transmit_interval_seconds"] or 0,
            cron=d["transmit_cron"],
            count=d["transmit_count"],
        ),
    )


def _fallback_policy(name: str) -> Policy:
    return Policy(
        name=name,
        fetch=Schedule(kind=_FALLBACK_FETCH_KIND, interval_seconds=DEFAULT_INTERVAL_SECONDS),
        transmit=Schedule(kind=_FALLBACK_TRANSMIT_KIND, count=_FALLBACK_TRANSMIT_COUNT),
    )


def get_policy_object(conn: sqlite3.Connection, name: str) -> Policy | None:
    """The full Policy for `name`, or None if there is no such row."""
    row = conn.execute(
        f"SELECT {', '.join(_POLICY_COLUMNS)} FROM policies WHERE name = ?",
        (name,),
    ).fetchone()
    return _row_to_policy(row) if row is not None else None


def resolve_policy(conn: sqlite3.Connection, name: str | None) -> Policy:
    """Resolve a policy name to its Policy, read fresh every call. Falls
    back to DEFAULT_POLICY_NAME if the name is missing/unset/unknown, and
    to a hardcoded Policy (interval fetch / air once) if even that row is
    gone — the pipeline never crashes on a management mistake."""
    if name:
        policy = get_policy_object(conn, name)
        if policy is not None:
            return policy

    default_policy = get_policy_object(conn, DEFAULT_POLICY_NAME)
    if default_policy is not None:
        return default_policy

    return _fallback_policy(name or DEFAULT_POLICY_NAME)


def policy_for(conn: sqlite3.Connection, name: str | None) -> Schedule:
    """The **transmit** Schedule a policy name currently points at — the
    lookup beacon does per drain cycle. Same fallback chain as
    resolve_policy."""
    return resolve_policy(conn, name).transmit


@dataclass(frozen=True)
class PolicyRow:
    """A flat view of one `policies` row for list/CRUD surfaces (the CLI
    and the ui's /config/policies page)."""

    name: str
    fetch_kind: str
    fetch_interval_seconds: int | None
    fetch_cron: str | None
    process_mode: str
    transmit_kind: str
    transmit_count: int
    transmit_interval_seconds: int
    transmit_cron: str | None
    description: str | None


def list_policies(conn: sqlite3.Connection) -> list[PolicyRow]:
    """Every `policies` row, ordered by name."""
    rows = conn.execute(
        f"SELECT {', '.join(_POLICY_COLUMNS)} FROM policies ORDER BY name"
    ).fetchall()
    return [PolicyRow(*row) for row in rows]


def get_policy(conn: sqlite3.Connection, name: str) -> PolicyRow | None:
    row = conn.execute(
        f"SELECT {', '.join(_POLICY_COLUMNS)} FROM policies WHERE name = ?",
        (name,),
    ).fetchone()
    return PolicyRow(*row) if row is not None else None


def _validate(
    fetch_kind: str,
    fetch_interval_seconds: int | None,
    fetch_cron: str | None,
    transmit_kind: str,
    transmit_count: int,
    transmit_interval_seconds: int,
    transmit_cron: str | None,
) -> tuple[int | None, str | None, int, int, str | None]:
    """Normalise + reject an incoherent Policy. Returns the cleaned
    (fetch_interval_seconds, fetch_cron, transmit_count,
    transmit_interval_seconds, transmit_cron)."""
    from adapters.cron import is_valid_cron

    if fetch_kind not in FETCH_KINDS:
        raise ValueError(f"fetch_kind must be one of {FETCH_KINDS}")
    if transmit_kind not in TRANSMIT_KINDS:
        raise ValueError(f"transmit_kind must be one of {TRANSMIT_KINDS}")

    if fetch_kind == "interval":
        if fetch_interval_seconds is not None and fetch_interval_seconds < 1:
            raise ValueError("fetch_interval_seconds must be >= 1")
        fetch_cron = None
    elif fetch_kind == "cron":
        if not is_valid_cron(fetch_cron or ""):
            raise ValueError(f"fetch_cron {fetch_cron!r} is not a valid cron expression")
        fetch_interval_seconds = None
    else:  # once
        fetch_interval_seconds = None
        fetch_cron = None

    if transmit_kind == "once":
        transmit_count = 1
        transmit_interval_seconds = 0
        transmit_cron = None
    elif transmit_kind == "interval":
        if transmit_count < 1:
            raise ValueError("transmit_count must be >= 1")
        if transmit_interval_seconds < 1:
            raise ValueError("transmit_interval_seconds must be >= 1")
        transmit_cron = None
    else:  # cron
        if transmit_count < 1:
            raise ValueError("transmit_count must be >= 1")
        if not is_valid_cron(transmit_cron or ""):
            raise ValueError(f"transmit_cron {transmit_cron!r} is not a valid cron expression")
        transmit_interval_seconds = 0

    return (
        fetch_interval_seconds,
        fetch_cron,
        transmit_count,
        transmit_interval_seconds,
        transmit_cron,
    )


def set_policy(
    conn: sqlite3.Connection,
    name: str,
    *,
    fetch_kind: str = "interval",
    fetch_interval_seconds: int | None = None,
    fetch_cron: str | None = None,
    process_mode: str = "on_new_data",
    transmit_kind: str = "once",
    transmit_count: int = 1,
    transmit_interval_seconds: int = 0,
    transmit_cron: str | None = None,
    description: str | None = None,
) -> None:
    """Create or replace a named Policy — the centralised management
    surface (dispatcher/policies.py, ui's /config/policies)."""
    (
        fetch_interval_seconds,
        fetch_cron,
        transmit_count,
        transmit_interval_seconds,
        transmit_cron,
    ) = _validate(
        fetch_kind,
        fetch_interval_seconds,
        fetch_cron,
        transmit_kind,
        transmit_count,
        transmit_interval_seconds,
        transmit_cron,
    )
    conn.execute(
        "INSERT INTO policies "
        "(name, fetch_kind, fetch_interval_seconds, fetch_cron, process_mode, "
        "transmit_kind, transmit_count, transmit_interval_seconds, transmit_cron, description) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
        "ON CONFLICT (name) DO UPDATE SET "
        "fetch_kind = excluded.fetch_kind, "
        "fetch_interval_seconds = excluded.fetch_interval_seconds, "
        "fetch_cron = excluded.fetch_cron, "
        "process_mode = excluded.process_mode, "
        "transmit_kind = excluded.transmit_kind, "
        "transmit_count = excluded.transmit_count, "
        "transmit_interval_seconds = excluded.transmit_interval_seconds, "
        "transmit_cron = excluded.transmit_cron, "
        "description = excluded.description",
        (
            name, fetch_kind, fetch_interval_seconds, fetch_cron, process_mode,
            transmit_kind, transmit_count, transmit_interval_seconds, transmit_cron, description,
        ),
    )
    conn.commit()
    record_audit_event(
        conn,
        event_type="policy.set",
        actor="adapters.policy",
        details={
            "name": name,
            "fetch_kind": fetch_kind,
            "fetch_interval_seconds": fetch_interval_seconds,
            "fetch_cron": fetch_cron,
            "transmit_kind": transmit_kind,
            "transmit_count": transmit_count,
            "transmit_interval_seconds": transmit_interval_seconds,
            "transmit_cron": transmit_cron,
            "description": description,
        },
    )


def delete_policy(conn: sqlite3.Connection, name: str) -> bool:
    """Returns whether a row was actually deleted (False if unknown)."""
    cursor = conn.execute("DELETE FROM policies WHERE name = ?", (name,))
    conn.commit()
    if cursor.rowcount > 0:
        record_audit_event(
            conn,
            event_type="policy.deleted",
            actor="adapters.policy",
            details={"name": name},
        )
    return cursor.rowcount > 0


def policy_reference_count(conn: sqlite3.Connection, name: str) -> dict[str, int]:
    """How many adapters and items currently name `name` — for a
    "still in use" warning before deletion."""
    adapters = conn.execute(
        "SELECT COUNT(*) FROM adapter_instances WHERE policy = ?", (name,)
    ).fetchone()[0]
    items = conn.execute(
        "SELECT COUNT(*) FROM items WHERE policy = ?", (name,)
    ).fetchone()[0]
    return {"adapters": adapters, "items": items}


def describe_policy(policy: Policy) -> str:
    """A one-line plain-language summary — "fetches every 10s · airs once",
    "fetches at 0 21 * * * · airs 2x at 0 7,19 * * *". Backs the adapters
    list / form "resolved rhythm" display."""

    def _fetch() -> str:
        s = policy.fetch
        if s.kind == "once":
            return "fetches once"
        if s.kind == "cron":
            return f"fetches on cron {s.cron}"
        return f"fetches every {s.interval_seconds}s"

    def _transmit() -> str:
        s = policy.transmit
        if s.kind == "once":
            return "airs once"
        if s.kind == "cron":
            return f"airs {s.count}x on cron {s.cron}"
        return f"airs {s.count}x every {s.interval_seconds}s"

    return f"{_fetch()} · {_transmit()}"
