import sqlite3
from dataclasses import dataclass

from adapters.storage import record_audit_event

DEFAULT_POLICY_NAME = "informational"

# Seeded into transmit_policies by adapters.storage._ensure_transmit_policies_seeded
# on first get_connection() call, only if the table is empty. A fresh install
# ships just `informational`; any further tier (e.g. an `urgent` at 5x/60s for
# escalated items) is added by an operator via dispatcher/policies.sh
# (list/set/delete) or the ui's /policies page, not by changing this constant.
SEED_POLICIES = (
    ("informational", 1, 0, "Transmitted once."),
)

# Absolute last-resort fallback if transmit_policies is empty or missing the
# DEFAULT_POLICY_NAME row (e.g. an operator deleted it) — transmission
# should never crash or loop forever from a management mistake.
_FALLBACK_POLICY_REPEAT_TIMES = 1
_FALLBACK_POLICY_INTERVAL_SECONDS = 0


@dataclass(frozen=True)
class RepeatPolicy:
    repeat_times: int
    interval_seconds: int


def get_policy(conn: sqlite3.Connection, name: str) -> RepeatPolicy | None:
    row = conn.execute(
        "SELECT repeat_times, interval_seconds FROM transmit_policies WHERE name = ?",
        (name,),
    ).fetchone()
    if row is None:
        return None
    return RepeatPolicy(repeat_times=row[0], interval_seconds=row[1])


def policy_for(conn: sqlite3.Connection, transmit_policy_name: str | None) -> RepeatPolicy:
    """Resolves an item's `transmit_policy` name (adapters.storage's generic
    contract column) to the RepeatPolicy it currently points at — read fresh
    every call, so editing a policy's numbers (or an item's transmit_policy)
    takes effect on the very next lookup. Falls back to DEFAULT_POLICY_NAME
    if the name is missing/unset/unknown, and to a hardcoded (1, 0) if even
    that policy is gone."""
    if transmit_policy_name:
        policy = get_policy(conn, transmit_policy_name)
        if policy is not None:
            return policy

    default_policy = get_policy(conn, DEFAULT_POLICY_NAME)
    if default_policy is not None:
        return default_policy

    return RepeatPolicy(
        repeat_times=_FALLBACK_POLICY_REPEAT_TIMES,
        interval_seconds=_FALLBACK_POLICY_INTERVAL_SECONDS,
    )


def list_policies(conn: sqlite3.Connection) -> list[tuple[str, int, int, str | None]]:
    """Returns (name, repeat_times, interval_seconds, description) rows,
    ordered by name."""
    return conn.execute(
        "SELECT name, repeat_times, interval_seconds, description "
        "FROM transmit_policies ORDER BY name"
    ).fetchall()


def set_policy(
    conn: sqlite3.Connection,
    name: str,
    repeat_times: int,
    interval_seconds: int,
    description: str | None = None,
) -> None:
    """Creates or replaces a named policy — the actual "centralized
    management" surface (see dispatcher/policies.py and ui's /policies)."""
    conn.execute(
        "INSERT INTO transmit_policies (name, repeat_times, interval_seconds, description) "
        "VALUES (?, ?, ?, ?) "
        "ON CONFLICT (name) DO UPDATE SET "
        "repeat_times = excluded.repeat_times, "
        "interval_seconds = excluded.interval_seconds, "
        "description = excluded.description",
        (name, repeat_times, interval_seconds, description),
    )
    conn.commit()
    record_audit_event(
        conn,
        event_type="policy.set",
        actor="adapters.transmit_policy",
        details={
            "name": name,
            "repeat_times": repeat_times,
            "interval_seconds": interval_seconds,
            "description": description,
        },
    )


def delete_policy(conn: sqlite3.Connection, name: str) -> bool:
    """Returns whether a row was actually deleted (False if unknown)."""
    cursor = conn.execute("DELETE FROM transmit_policies WHERE name = ?", (name,))
    conn.commit()
    if cursor.rowcount > 0:
        record_audit_event(
            conn,
            event_type="policy.deleted",
            actor="adapters.transmit_policy",
            details={"name": name},
        )
    return cursor.rowcount > 0
