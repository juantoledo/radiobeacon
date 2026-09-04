"""Validates ad hoc SQL for the Developers SQL runner before it ever
reaches SQLite. This is defense in depth, not the actual safety boundary
— that's db.open_readonly_connection's mode=ro connection, which SQLite
itself enforces at the driver level regardless of what this validation
catches or misses. This module exists to turn an obviously-wrong query
into a clear message ("only SELECT is allowed") instead of a confusing
raw sqlite3.OperationalError."""
import re

_DISALLOWED_KEYWORDS = re.compile(
    r"\b(insert|update|delete|drop|alter|attach|detach|pragma|vacuum|replace|create|reindex)\b",
    re.IGNORECASE,
)

# The `settings` table stores API keys (ANTHROPIC_API_KEY, OPENAI_API_KEY, ...)
# in plaintext; the config UI only masks them on the way out. The SQL runner
# would otherwise be a straight read path around that masking, so any query
# that so much as names the table is refused here — crude on purpose (it also
# trips on the bare word in a string literal or another table's column), with a
# message that says why. This is the same defense-in-depth layer as the rest of
# this module, not the real boundary.
_BLOCKED_TABLES = re.compile(r"\bsettings\b", re.IGNORECASE)


class InvalidQuery(ValueError):
    pass


def ensure_select_only(sql: str) -> str:
    """Returns the trimmed, single-statement query text if it looks like
    a read-only SELECT (or WITH ... SELECT common table expression);
    raises InvalidQuery otherwise. A trailing semicolon is tolerated and
    stripped; a semicolon anywhere else means more than one statement,
    which is rejected outright rather than silently running only the
    first."""
    stripped = sql.strip()
    if stripped.endswith(";"):
        stripped = stripped[:-1].strip()

    if not stripped:
        raise InvalidQuery("enter a query")
    if ";" in stripped:
        raise InvalidQuery("only a single statement is allowed")

    lowered = stripped.lower()
    if not (lowered.startswith("select") or lowered.startswith("with")):
        raise InvalidQuery("only SELECT (or WITH ... SELECT) queries are allowed")

    if _DISALLOWED_KEYWORDS.search(stripped):
        raise InvalidQuery("query contains a disallowed keyword")

    if _BLOCKED_TABLES.search(stripped):
        raise InvalidQuery("the settings table is not queryable here (it holds secrets)")

    return stripped
