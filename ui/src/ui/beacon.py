"""Beacon identity — the operator profile (callsign, description, etc.)
for CD3DXZ-1, editable only via /beacon, never via .env/os.environ (see
get_setting/set_setting calls in routers/beacon.py, which always pass
env_fallback=False). Presentation + domain-rule metadata only, same split
as config_catalog.py: storage itself is still adapters.storage.get_setting/
set_setting/delete_setting, keyed by these BEACON_* names. These are NOT
real env vars (unlike every other settings key) — the BEACON_ prefix is
kept only for naming-convention consistency with the rest of the `settings`
table, and to make misuse (someone setting a BEACON_* env var expecting it
to take effect) inert rather than silently working."""
from dataclasses import dataclass
import sqlite3


@dataclass(frozen=True)
class BeaconField:
    key: str
    label: str
    description: str


# All fields are required — the beacon is not considered "configured"
# until every one of these has a non-blank value.
BEACON_FIELDS: list[BeaconField] = [
    BeaconField(
        "BEACON_CALLSIGN",
        "Callsign",
        "The beacon's amateur radio callsign, e.g. CD3DXZ-1.",
    ),
    BeaconField(
        "BEACON_DESCRIPTION",
        "Description",
        "Longer description of the beacon/project.",
    ),
    BeaconField(
        "BEACON_SHORT_DESCRIPTION",
        "Short description",
        "One-line summary, e.g. for compact displays.",
    ),
    BeaconField(
        "BEACON_OPERATOR_CONTACT",
        "Operator contact",
        "How to reach the operator (email, etc.).",
    ),
    BeaconField(
        "BEACON_GRID_LOCATOR",
        "Grid locator (QTH)",
        "Maidenhead grid square, e.g. FF46vb.",
    ),
    BeaconField(
        "BEACON_FREQUENCY",
        "Frequency",
        "VHF transmit frequency, e.g. 144.390 MHz.",
    ),
]

REQUIRED_BEACON_KEYS: tuple[str, ...] = tuple(f.key for f in BEACON_FIELDS)


def is_beacon_configured(conn: sqlite3.Connection) -> bool:
    """True once every field in BEACON_FIELDS has a non-blank DB row.
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
