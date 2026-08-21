import sqlite3
from dataclasses import dataclass

DEFAULT_POLICY_NAME = "informational"

# Seeded into dispatch_policies on first _ensure_tables() call, only if
# the table is empty — the starting set an operator can then edit via
# triggers/policies.sh (list/set/delete), not by changing these constants.
_SEED_POLICIES = (
    ("urgent", 5, 60, "Redelivers several times, spread out."),
    ("informational", 1, 0, "Delivered once."),
)

# Absolute last-resort fallback if dispatch_policies is empty or missing
# the DEFAULT_POLICY_NAME row (e.g. an operator deleted it) — delivery
# should never crash or loop forever from a management mistake.
_FALLBACK_POLICY_REPEAT_TIMES = 1
_FALLBACK_POLICY_INTERVAL_SECONDS = 0


@dataclass(frozen=True)
class RepeatPolicy:
    repeat_times: int
    interval_seconds: int


def ensure_seeded(conn: sqlite3.Connection) -> None:
    count = conn.execute("SELECT COUNT(*) FROM dispatch_policies").fetchone()[0]
    if count == 0:
        conn.executemany(
            "INSERT INTO dispatch_policies (name, repeat_times, interval_seconds, description) "
            "VALUES (?, ?, ?, ?)",
            _SEED_POLICIES,
        )
        conn.commit()


def get_policy(conn: sqlite3.Connection, name: str) -> RepeatPolicy | None:
    row = conn.execute(
        "SELECT repeat_times, interval_seconds FROM dispatch_policies WHERE name = ?",
        (name,),
    ).fetchone()
    if row is None:
        return None
    return RepeatPolicy(repeat_times=row[0], interval_seconds=row[1])


def policy_for(conn: sqlite3.Connection, dispatch_policy_name: str | None) -> RepeatPolicy:
    """Resolves an item's `dispatch_policy` name (adapters.storage's
    generic contract column) to the RepeatPolicy it currently points at
    — read fresh every call, so editing a policy's numbers (or an item's
    dispatch_policy) takes effect on the very next lookup. Falls back to
    DEFAULT_POLICY_NAME if the name is missing/unset/unknown, and to a
    hardcoded (1, 0) if even that policy is gone."""
    if dispatch_policy_name:
        policy = get_policy(conn, dispatch_policy_name)
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
        "FROM dispatch_policies ORDER BY name"
    ).fetchall()


def set_policy(
    conn: sqlite3.Connection,
    name: str,
    repeat_times: int,
    interval_seconds: int,
    description: str | None = None,
) -> None:
    """Creates or replaces a named policy — the actual "centralized
    management" surface (see triggers/policies.py)."""
    conn.execute(
        "INSERT INTO dispatch_policies (name, repeat_times, interval_seconds, description) "
        "VALUES (?, ?, ?, ?) "
        "ON CONFLICT (name) DO UPDATE SET "
        "repeat_times = excluded.repeat_times, "
        "interval_seconds = excluded.interval_seconds, "
        "description = excluded.description",
        (name, repeat_times, interval_seconds, description),
    )
    conn.commit()


def delete_policy(conn: sqlite3.Connection, name: str) -> bool:
    """Returns whether a row was actually deleted (False if unknown)."""
    cursor = conn.execute("DELETE FROM dispatch_policies WHERE name = ?", (name,))
    conn.commit()
    return cursor.rowcount > 0
