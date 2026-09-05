"""Login accounts, password hashing, and session lifecycle for ui/'s
authentication — sits on top of storage.py's users/sessions schema the same
way adapters.transmit_policy sits on top of _CREATE_TRANSMIT_POLICIES.

hashlib.scrypt (stdlib since Python 3.6) is used instead of bcrypt/argon2/
passlib: nothing else in this repo pulls in a password-hashing dependency,
and scrypt is memory-hard and cost-tunable, which is what actually matters
for a login form."""
import hashlib
import secrets
import sqlite3
from dataclasses import dataclass

from .storage import (
    _ensure_sessions_table,
    _ensure_users_table,
    record_audit_event,
)

# OWASP's minimum recommended interactive-login cost as of this writing.
# Embedded into every stored hash (see hash_password) so raising these
# later doesn't invalidate already-stored hashes — verify_password reads
# the parameters back out of the string instead of assuming today's
# constants.
_SCRYPT_N = 2**14
_SCRYPT_R = 8
_SCRYPT_P = 1
_SCRYPT_DKLEN = 32
_SALT_BYTES = 16

# 30-day sliding session: long enough that a shack-network operator isn't
# logged out mid-week, refreshed only once it's more than half spent so an
# auto-refreshing dashboard doesn't turn every single request into a
# sessions-table write.
SESSION_TTL_SECONDS = 30 * 24 * 3600
SESSION_REFRESH_THRESHOLD_SECONDS = 15 * 24 * 3600

VALID_ROLES = ("admin", "user")


def hash_password(password: str) -> str:
    """Returns a self-describing 'scrypt$N$r$p$<salt_hex>$<hash_hex>'
    string — the cost parameters travel with the hash, so verify_password
    never has to assume they match this module's current constants."""
    salt = secrets.token_bytes(_SALT_BYTES)
    digest = hashlib.scrypt(
        password.encode("utf-8"), salt=salt, n=_SCRYPT_N, r=_SCRYPT_R, p=_SCRYPT_P, dklen=_SCRYPT_DKLEN
    )
    return f"scrypt${_SCRYPT_N}${_SCRYPT_R}${_SCRYPT_P}${salt.hex()}${digest.hex()}"


def verify_password(password: str, encoded: str) -> bool:
    """Never raises — a malformed/foreign-format `encoded` value (e.g. a
    hand-edited DB row) just fails verification rather than crashing the
    login page."""
    try:
        algo, n, r, p, salt_hex, hash_hex = encoded.split("$")
        if algo != "scrypt":
            return False
        salt = bytes.fromhex(salt_hex)
        expected = bytes.fromhex(hash_hex)
    except (ValueError, AttributeError):
        return False
    digest = hashlib.scrypt(
        password.encode("utf-8"),
        salt=salt,
        n=int(n),
        r=int(r),
        p=int(p),
        dklen=len(expected),
    )
    return secrets.compare_digest(digest, expected)


@dataclass(frozen=True)
class User:
    id: int
    username: str
    role: str
    disabled: bool


def _row_to_user(row: tuple) -> User:
    """Positional, not name-based — `row` may be a plain tuple (the
    default row_factory get_connection() leaves in place for a caller that
    never opts into sqlite3.Row, e.g. a CLI script or app.py's own
    bootstrap connection), matching every SELECT below's fixed column
    order (id, username, role, disabled)."""
    return User(id=row[0], username=row[1], role=row[2], disabled=bool(row[3]))


def get_user(conn: sqlite3.Connection, user_id: int) -> User | None:
    _ensure_users_table(conn)
    row = conn.execute(
        "SELECT id, username, role, disabled FROM users WHERE id = ?", (user_id,)
    ).fetchone()
    return None if row is None else _row_to_user(row)


def get_user_by_username(conn: sqlite3.Connection, username: str) -> User | None:
    _ensure_users_table(conn)
    row = conn.execute(
        "SELECT id, username, role, disabled FROM users WHERE username = ?", (username,)
    ).fetchone()
    return None if row is None else _row_to_user(row)


def list_users(conn: sqlite3.Connection) -> list[User]:
    _ensure_users_table(conn)
    rows = conn.execute(
        "SELECT id, username, role, disabled FROM users ORDER BY username"
    ).fetchall()
    return [_row_to_user(row) for row in rows]


def any_admin_exists(conn: sqlite3.Connection) -> bool:
    """Deliberately counts a *disabled* admin too — this only gates
    bootstrap_admin_if_missing, and if it only checked *enabled* admins, a
    sole admin account that got disabled would make every restart retry
    create_user(..., "admin", ...) and crash on the username's UNIQUE
    constraint instead of quietly no-opping. An operator who disables
    their only admin is expected to fix that via manage_users.sh, not
    have bootstrap silently fabricate a second one."""
    _ensure_users_table(conn)
    row = conn.execute("SELECT 1 FROM users WHERE role = 'admin' LIMIT 1").fetchone()
    return row is not None


def create_user(
    conn: sqlite3.Connection, username: str, password: str, role: str, *, actor: str
) -> User:
    """Raises sqlite3.IntegrityError on a duplicate username (caller's
    problem to turn into a friendly form error, same as adapter_instances'
    UNIQUE(source) does today)."""
    if role not in VALID_ROLES:
        raise ValueError(f"invalid role {role!r}")
    _ensure_users_table(conn)
    cursor = conn.execute(
        "INSERT INTO users (username, password_hash, role) VALUES (?, ?, ?)",
        (username, hash_password(password), role),
    )
    conn.commit()
    record_audit_event(
        conn, event_type="user.created", actor=actor, details={"username": username, "role": role}
    )
    return User(id=cursor.lastrowid, username=username, role=role, disabled=False)


def set_user_password(conn: sqlite3.Connection, user_id: int, new_password: str, *, actor: str) -> bool:
    _ensure_users_table(conn)
    cursor = conn.execute(
        "UPDATE users SET password_hash = ?, updated_at = datetime('now') WHERE id = ?",
        (hash_password(new_password), user_id),
    )
    conn.commit()
    if cursor.rowcount == 0:
        return False
    delete_all_sessions_for_user(conn, user_id)
    record_audit_event(conn, event_type="user.password_changed", actor=actor, details={"user_id": user_id})
    return True


def set_user_role(conn: sqlite3.Connection, user_id: int, role: str, *, actor: str) -> bool:
    if role not in VALID_ROLES:
        raise ValueError(f"invalid role {role!r}")
    _ensure_users_table(conn)
    cursor = conn.execute(
        "UPDATE users SET role = ?, updated_at = datetime('now') WHERE id = ?", (role, user_id)
    )
    conn.commit()
    if cursor.rowcount == 0:
        return False
    delete_all_sessions_for_user(conn, user_id)
    record_audit_event(
        conn, event_type="user.role_changed", actor=actor, details={"user_id": user_id, "role": role}
    )
    return True


def delete_user(conn: sqlite3.Connection, user_id: int, *, actor: str) -> bool:
    """Deletes the user's sessions first, then the user row — the explicit
    cascade this FK-less schema needs (see storage.py's _CREATE_USERS)."""
    _ensure_users_table(conn)
    delete_all_sessions_for_user(conn, user_id)
    cursor = conn.execute("DELETE FROM users WHERE id = ?", (user_id,))
    conn.commit()
    if cursor.rowcount == 0:
        return False
    record_audit_event(conn, event_type="user.deleted", actor=actor, details={"user_id": user_id})
    return True


def bootstrap_admin_if_missing(conn: sqlite3.Connection, *, actor: str = "system.bootstrap") -> tuple[str, str] | None:
    """Creates username='admin' with a freshly generated password only if
    no enabled admin account exists yet; returns (username, password) in
    that case, else None. "does an admin row exist" IS the idempotency
    check — same idiom as _ensure_adapter_instances_seeded (seed once,
    checked by row presence, no separate marker)."""
    if any_admin_exists(conn):
        return None
    password = secrets.token_urlsafe(18)
    create_user(conn, "admin", password, "admin", actor=actor)
    return "admin", password


def _hash_token(raw_token: str) -> str:
    return hashlib.sha256(raw_token.encode("utf-8")).hexdigest()


def create_session(conn: sqlite3.Connection, user_id: int) -> str:
    """Returns the RAW token — the only time it's ever available in full;
    only sha256(raw token) is stored. Caller sets it as the session
    cookie."""
    _ensure_sessions_table(conn)
    raw_token = secrets.token_urlsafe(32)
    conn.execute(
        "INSERT INTO sessions (token_hash, user_id, expires_at) "
        "VALUES (?, ?, datetime('now', ?))",
        (_hash_token(raw_token), user_id, f"+{SESSION_TTL_SECONDS} seconds"),
    )
    conn.commit()
    return raw_token


def validate_session(conn: sqlite3.Connection, raw_token: str) -> User | None:
    """Looks up sha256(raw_token); returns None if missing/expired/the
    owning user is disabled. On a hit, bumps last_seen_at always, and
    slides expires_at forward when fewer than
    SESSION_REFRESH_THRESHOLD_SECONDS remain — a sliding window without a
    write on every single request. Opportunistically prunes expired rows
    on the same call, so the table never needs a separate cleanup job."""
    _ensure_sessions_table(conn)
    token_hash = _hash_token(raw_token)
    conn.execute("DELETE FROM sessions WHERE expires_at < datetime('now')")
    row = conn.execute(
        "SELECT u.id, u.username, u.role, u.disabled "
        "FROM sessions s JOIN users u ON u.id = s.user_id "
        "WHERE s.token_hash = ? AND s.expires_at >= datetime('now')",
        (token_hash,),
    ).fetchone()
    if row is None:
        conn.commit()
        return None
    user = _row_to_user(row)
    if user.disabled:
        conn.commit()
        return None

    remaining = conn.execute(
        "SELECT (julianday(expires_at) - julianday('now')) * 86400 "
        "FROM sessions WHERE token_hash = ?",
        (token_hash,),
    ).fetchone()[0]
    if remaining is not None and remaining < SESSION_REFRESH_THRESHOLD_SECONDS:
        conn.execute(
            "UPDATE sessions SET last_seen_at = datetime('now'), "
            "expires_at = datetime('now', ?) WHERE token_hash = ?",
            (f"+{SESSION_TTL_SECONDS} seconds", token_hash),
        )
    else:
        conn.execute(
            "UPDATE sessions SET last_seen_at = datetime('now') WHERE token_hash = ?", (token_hash,)
        )
    conn.commit()
    return user


def delete_session(conn: sqlite3.Connection, raw_token: str) -> None:
    _ensure_sessions_table(conn)
    conn.execute("DELETE FROM sessions WHERE token_hash = ?", (_hash_token(raw_token),))
    conn.commit()


def delete_all_sessions_for_user(conn: sqlite3.Connection, user_id: int) -> None:
    _ensure_sessions_table(conn)
    conn.execute("DELETE FROM sessions WHERE user_id = ?", (user_id,))
    conn.commit()
