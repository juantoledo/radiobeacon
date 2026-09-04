"""Request-gating for the dashboard — it has no auth of its own, so this is
the layer that keeps a page open in the operator's browser (or anything else
on the network) from driving it.

- `TrustedHostMiddleware` (wired in app.py) rejects a Host header that isn't
  in UI_ALLOWED_HOSTS — a DNS-rebinding guard.
- `CrossOriginGuardMiddleware` rejects an unsafe request (POST/PUT/PATCH/
  DELETE) whose `Origin` names a host that isn't allow-listed. Browsers
  always attach `Origin` to a cross-site POST, so this stops the classic
  "malicious site auto-submits a form to http://127.0.0.1:8080" attack.
  Requests with no `Origin` (curl, server-to-server) are left alone — they
  can't be driven by a third-party web page, and the loopback bind fences
  them.
- `CsrfCookieMiddleware` + `verify_csrf` add a double-submit-cookie token on
  top: every response carries a `csrf_token` cookie, every state-changing
  form must echo it back (hidden field `csrf_token`, or an `X-CSRF-Token`
  header). Defense in depth behind the Origin check, and the part that still
  holds if a browser ever omits `Origin`.
"""
import secrets
from urllib.parse import urlsplit

from fastapi import HTTPException
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import PlainTextResponse

_UNSAFE_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})

CSRF_COOKIE_NAME = "csrf_token"
CSRF_FIELD_NAME = "csrf_token"
CSRF_HEADER_NAME = "x-csrf-token"


class CrossOriginGuardMiddleware(BaseHTTPMiddleware):
    def __init__(self, app, *, allowed_hosts: list[str]) -> None:
        super().__init__(app)
        # Host names only (no scheme/port); an Origin's host is compared
        # against this set. "*" disables the check.
        self._allowed = {h.lstrip("*.") for h in allowed_hosts}
        self._allow_all = "*" in allowed_hosts

    async def dispatch(self, request: Request, call_next):
        if request.method in _UNSAFE_METHODS and not self._allow_all:
            origin = request.headers.get("origin")
            if origin is not None:
                origin_host = urlsplit(origin).hostname or ""
                if origin_host not in self._allowed:
                    return PlainTextResponse(
                        "cross-origin request blocked", status_code=403
                    )
        return await call_next(request)


class CsrfCookieMiddleware(BaseHTTPMiddleware):
    """Ensures every response carries a `csrf_token` cookie (minting one when
    the request arrived without it) and stashes the value on
    `request.state.csrf_token` for the templates' hidden field."""

    async def dispatch(self, request: Request, call_next):
        existing = request.cookies.get(CSRF_COOKIE_NAME)
        token = existing or secrets.token_urlsafe(32)
        request.state.csrf_token = token
        response = await call_next(request)
        if existing != token:
            response.set_cookie(
                CSRF_COOKIE_NAME,
                token,
                httponly=False,  # the form's hidden field is filled server-side; JS never needs it
                samesite="strict",
                path="/",
            )
        return response


async def verify_csrf(request: Request) -> None:
    """App-wide dependency: an unsafe request must echo the csrf_token cookie
    back in the `csrf_token` form field or an `X-CSRF-Token` header. No-ops on
    safe methods. Overridden to a no-op in the test client fixture (with
    dedicated tests covering the real path), the same way Django's test
    client disables CSRF."""
    if request.method not in _UNSAFE_METHODS:
        return
    cookie = request.cookies.get(CSRF_COOKIE_NAME)
    sent = request.headers.get(CSRF_HEADER_NAME)
    if sent is None:
        content_type = request.headers.get("content-type", "")
        if content_type.startswith(
            ("application/x-www-form-urlencoded", "multipart/form-data")
        ):
            form = await request.form()
            value = form.get(CSRF_FIELD_NAME)
            sent = value if isinstance(value, str) else None
    if not cookie or not sent or not secrets.compare_digest(cookie, sent):
        raise HTTPException(status_code=403, detail="CSRF token missing or invalid")
