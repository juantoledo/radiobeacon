"""Per-browser "seen" marker for the dashboard's failed-events banner —
modeled on theme.py's cookie-only pattern (no DB row, no audit_log noise
from checking your own audit log). Written only when an admin follows the
banner's own "check the audit log" link (routers/audit.py, on `status=
failed`), read by the dashboard (routers/dashboard.py) to hide already-
acknowledged failures and show the banner again only once a genuinely new
one is recorded after that point."""
from starlette.requests import Request

AUDIT_ACK_COOKIE_NAME = "audit_ack_id"


def resolve_ack_id(request: Request) -> int:
    """0 (no floor) when the cookie is absent or unparseable — the banner
    then behaves exactly as before, showing every failure in the window."""
    raw = request.cookies.get(AUDIT_ACK_COOKIE_NAME)
    try:
        return int(raw)
    except (TypeError, ValueError):
        return 0
