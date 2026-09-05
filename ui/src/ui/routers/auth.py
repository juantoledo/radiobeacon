"""Login/logout — the one deliberately public surface besides /static (see
ui.current_user for the session-cookie mechanics /login itself relies on
to know whether a visitor is already logged in)."""
import sqlite3

from adapters.auth import (
    SESSION_TTL_SECONDS,
    create_session,
    delete_session,
    get_user_by_username,
    validate_session,
    verify_password,
)
from adapters.storage import record_audit_event
from fastapi import APIRouter, Depends, Form, Request
from starlette.responses import RedirectResponse

from ..current_user import SESSION_COOKIE_NAME, get_current_user
from ..db import get_db
from ..templating import templates

router = APIRouter()


def _safe_next(next_path: str | None) -> str:
    """Only ever redirect to a same-origin relative path — copies
    ui.routers.locale's own `next` guard, plus rejecting a leading `//`
    (which a browser resolves as protocol-relative, i.e. still an open
    redirect) since locale's guard predates that check."""
    if not next_path or not next_path.startswith("/") or next_path.startswith("//"):
        return "/"
    return next_path


@router.get("/login")
def login_page(request: Request, next: str = "/", conn: sqlite3.Connection = Depends(get_db)):
    token = request.cookies.get(SESSION_COOKIE_NAME)
    if token and validate_session(conn, token) is not None:
        return RedirectResponse(url=_safe_next(next), status_code=303)
    return templates.TemplateResponse(
        request, "login.html", {"next": _safe_next(next), "error": None}
    )


@router.post("/login")
def login_action(
    request: Request,
    username: str = Form(...),
    password: str = Form(...),
    next: str = Form("/"),
    conn: sqlite3.Connection = Depends(get_db),
):
    next_path = _safe_next(next)
    user = get_user_by_username(conn, username)
    if user is None or user.disabled or not verify_password(password, _password_hash(conn, user.id)):
        record_audit_event(
            conn, event_type="user.login_failed", actor="ui.auth", details={"username": username}
        )
        return templates.TemplateResponse(
            request,
            "login.html",
            {"next": next_path, "error": "invalid username or password"},
            status_code=400,
        )

    raw_token = create_session(conn, user.id)
    record_audit_event(
        conn, event_type="user.login", actor="ui.auth", details={"user_id": user.id, "username": user.username}
    )
    response = RedirectResponse(url=next_path, status_code=303)
    response.set_cookie(
        SESSION_COOKIE_NAME,
        raw_token,
        httponly=True,
        samesite="strict",
        path="/",
        max_age=SESSION_TTL_SECONDS,
    )
    return response


def _password_hash(conn: sqlite3.Connection, user_id: int) -> str:
    """get_user_by_username intentionally doesn't expose password_hash on
    User (nothing else should ever need it) — this one-off lookup keeps
    that field out of the dataclass entirely."""
    row = conn.execute("SELECT password_hash FROM users WHERE id = ?", (user_id,)).fetchone()
    return row[0] if row else ""


@router.post("/logout")
def logout_action(request: Request, user=Depends(get_current_user), conn: sqlite3.Connection = Depends(get_db)):
    token = request.cookies.get(SESSION_COOKIE_NAME)
    if token:
        delete_session(conn, token)
    record_audit_event(conn, event_type="user.logout", actor="ui.auth", details={"user_id": user.id})
    response = RedirectResponse(url="/login", status_code=303)
    response.delete_cookie(SESSION_COOKIE_NAME, path="/")
    return response
