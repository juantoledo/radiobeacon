"""Session-cookie auth: who's logged in, what role they have, and the
require_login / require_role("admin") dependencies every router (other than
ui.routers.auth's own /login) is gated with.

A FastAPI dependency, not ASGI middleware, even though it conceptually sits
alongside the middleware stack in ui.security — middleware runs before
Depends(get_db) resolves, so it can't reuse request.state.db_conn / the
test client's get_db override (same constraint documented on
ui.templating's _beacon_configured_global), and session validation needs
exactly that DB access to see whatever the current request's connection
holds."""
import sqlite3

from adapters.auth import User, validate_session
from fastapi import Depends, HTTPException, Request

from .db import get_db

SESSION_COOKIE_NAME = "session_token"


class NotAuthenticated(Exception):
    """Raised by get_current_user when there's no valid session; caught by
    an app.py exception handler that 303-redirects to /login?next=<path>."""

    def __init__(self, next_path: str) -> None:
        self.next_path = next_path


def get_current_user(request: Request, conn: sqlite3.Connection = Depends(get_db)) -> User:
    token = request.cookies.get(SESSION_COOKIE_NAME)
    user = validate_session(conn, token) if token else None
    if user is None or user.disabled:
        raise NotAuthenticated(next_path=request.url.path)
    # Stashed so templating.py's current_user() Jinja global (and the
    # NotAuthenticated handler's own logging, if ever needed) can read it
    # back without a second session lookup.
    request.state.user = user
    return user


def require_role(role: str):
    """APIRouter(dependencies=[Depends(require_role("admin"))]) — matches
    ui.routers.dev's _require_dev_tools_enabled precedent: a router-level
    dependency, not per-route. Login-checking (get_current_user) and
    role-checking are one dependency call, so a gated router only needs
    this single line."""

    def _dependency(user: User = Depends(get_current_user)) -> User:
        if user.role != role:
            raise HTTPException(status_code=403, detail="admin access required")
        return user

    return _dependency
