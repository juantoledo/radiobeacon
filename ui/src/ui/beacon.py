"""Beacon identity — the operator profile (callsign, description, etc.)
for CD3DXZ-1. Lives in the same DB-backed settings catalog as every other
setting (see config_catalog.py's "Beacon — Identity" group), editable via
/config/beacon-identity — env_fallback=False there keeps these DB-only,
never real env vars, so a stray BEACON_CALLSIGN in the process
environment can never silently take effect.

is_beacon_configured() stays here rather than config_catalog.py since
it's a domain rule ("is the beacon ready to transmit"), not settings
metadata — consumed sitewide (nav badge, banner, item override/rearm
gating)."""
import sqlite3

from .config_catalog import specs_for_group

REQUIRED_BEACON_KEYS: tuple[str, ...] = tuple(
    spec.key for spec in specs_for_group("beacon-identity")
)


def is_beacon_configured(conn: sqlite3.Connection) -> bool:
    """True once every key in REQUIRED_BEACON_KEYS has a non-blank DB row.
    Assumes the `settings` table already exists on this conn — every conn
    reaching this function came from adapters.storage.get_connection via
    ui.db.get_db (or templating.py's own short-lived connection), both of
    which already create/migrate it — same assumption ui.queries makes."""
    placeholders = ",".join("?" for _ in REQUIRED_BEACON_KEYS)
    row = conn.execute(
        f"SELECT COUNT(*) FROM settings WHERE key IN ({placeholders}) "
        "AND value IS NOT NULL AND TRIM(value) != ''",
        REQUIRED_BEACON_KEYS,
    ).fetchone()
    return row[0] == len(REQUIRED_BEACON_KEYS)
