import sqlite3
from urllib.parse import urlencode

from adapters.auth import (
    VALID_ROLES,
    User,
    create_user,
    delete_user,
    get_user,
    list_users,
    set_user_password,
    set_user_role,
)
from fastapi import APIRouter, Depends, Form, HTTPException, Request
from starlette.responses import RedirectResponse

from ..ajax import ajax_error, ajax_ok, is_ajax
from ..current_user import require_role
from ..db import get_db
from ..fragments import render_fragment
from ..templating import templates

router = APIRouter(dependencies=[Depends(require_role("admin"))])


def _enabled_admin_count(conn: sqlite3.Connection) -> int:
    return sum(1 for u in list_users(conn) if u.role == "admin" and not u.disabled)


def _users_list_context(conn: sqlite3.Connection) -> dict:
    """The /users listing's template context — shared by the GET page and
    the role-change/reset-password/delete routes' AJAX success branches
    below."""
    return {"users": list_users(conn)}


@router.get("/users")
def users_list_page(request: Request, conn: sqlite3.Connection = Depends(get_db)):
    return templates.TemplateResponse(request, "users_list.html", _users_list_context(conn))


def _render_users_list(request: Request, conn: sqlite3.Connection) -> str:
    return render_fragment(request, "users_list.html", _users_list_context(conn))


@router.get("/users/new")
def user_new_page(request: Request):
    return templates.TemplateResponse(
        request, "users_form.html", {"error": None, "username": "", "role": "user"}
    )


@router.post("/users")
def user_create_action(
    request: Request,
    username: str = Form(...),
    password: str = Form(...),
    confirm_password: str = Form(...),
    role: str = Form(...),
    conn: sqlite3.Connection = Depends(get_db),
):
    username = username.strip()
    error = None
    if role not in VALID_ROLES:
        error = "invalid role"
    elif not username:
        error = "username is required"
    elif password != confirm_password:
        error = "passwords do not match"
    elif len(password) < 8:
        error = "password must be at least 8 characters"

    if error:
        return templates.TemplateResponse(
            request,
            "users_form.html",
            {"error": error, "username": username, "role": role},
            status_code=400,
        )

    try:
        create_user(conn, username, password, role, actor="ui.users")
    except sqlite3.IntegrityError:
        return templates.TemplateResponse(
            request,
            "users_form.html",
            {"error": f"username {username!r} is already taken", "username": username, "role": role},
            status_code=400,
        )

    return RedirectResponse(
        url=f"/users?{urlencode({'msg': f'user {username!r} created'})}", status_code=303
    )


def _get_user_or_404(conn: sqlite3.Connection, user_id: int) -> User:
    user = get_user(conn, user_id)
    if user is None:
        raise HTTPException(status_code=404, detail="user not found")
    return user


@router.get("/users/{user_id}/edit")
def user_edit_page(request: Request, user_id: int, conn: sqlite3.Connection = Depends(get_db)):
    user = _get_user_or_404(conn, user_id)
    return templates.TemplateResponse(request, "users_edit.html", {"user": user, "error": None})


@router.post("/users/{user_id}/role")
def user_role_action(
    request: Request, user_id: int, role: str = Form(...), conn: sqlite3.Connection = Depends(get_db)
):
    user = _get_user_or_404(conn, user_id)
    if role not in VALID_ROLES:
        raise HTTPException(status_code=400, detail="invalid role")
    if user.role == "admin" and role == "user" and _enabled_admin_count(conn) <= 1:
        error = "cannot demote the last remaining admin"
        if is_ajax(request):
            return ajax_error(error)
        return templates.TemplateResponse(
            request,
            "users_edit.html",
            {"user": user, "error": error},
            status_code=400,
        )
    set_user_role(conn, user_id, role, actor="ui.users")
    msg = f"{user.username!r} is now {role}"
    if is_ajax(request):
        return ajax_ok(msg, fragment=_render_users_list(request, conn))
    return RedirectResponse(url=f"/users?{urlencode({'msg': msg})}", status_code=303)


@router.post("/users/{user_id}/reset-password")
def user_reset_password_action(
    request: Request,
    user_id: int,
    password: str = Form(...),
    confirm_password: str = Form(...),
    conn: sqlite3.Connection = Depends(get_db),
):
    user = _get_user_or_404(conn, user_id)
    if password != confirm_password:
        error = "passwords do not match"
        if is_ajax(request):
            return ajax_error(error)
        return templates.TemplateResponse(
            request,
            "users_edit.html",
            {"user": user, "error": error},
            status_code=400,
        )
    if len(password) < 8:
        error = "password must be at least 8 characters"
        if is_ajax(request):
            return ajax_error(error)
        return templates.TemplateResponse(
            request,
            "users_edit.html",
            {"user": user, "error": error},
            status_code=400,
        )
    set_user_password(conn, user_id, password, actor="ui.users")
    msg = f"password reset for {user.username!r}"
    if is_ajax(request):
        return ajax_ok(msg, fragment=_render_users_list(request, conn))
    return RedirectResponse(url=f"/users?{urlencode({'msg': msg})}", status_code=303)


@router.post("/users/{user_id}/delete")
def user_delete_action(request: Request, user_id: int, conn: sqlite3.Connection = Depends(get_db)):
    user = _get_user_or_404(conn, user_id)
    if user.role == "admin" and _enabled_admin_count(conn) <= 1:
        error = "cannot delete the last remaining admin"
        if is_ajax(request):
            return ajax_error(error)
        return templates.TemplateResponse(
            request,
            "users_list.html",
            {"users": list_users(conn), "error": error},
            status_code=400,
        )
    delete_user(conn, user_id, actor="ui.users")
    msg = f"{user.username!r} deleted"
    if is_ajax(request):
        return ajax_ok(msg, fragment=_render_users_list(request, conn))
    return RedirectResponse(url=f"/users?{urlencode({'msg': msg})}", status_code=303)
