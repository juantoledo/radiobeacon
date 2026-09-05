"""The "Beacon — SvxLink" and "Beacon — Direwolf" config pages.

Each renders the standard catalog group form (via
ui.routers.config.build_group_form_context) PLUS an optional raw editor for
the real svxlink.conf / direwolf.conf file on the host. The editor is OFF by
default and gated on BEACON_RF_CONF_EDITOR_ENABLED, on top of the admin-only
login gate every route under /config already requires: an always-on "write
any path the UI process can reach" surface is the same risk class as CUSTOM
adapters or the /dev tools (both flag-gated too), worth a second switch even
behind a login.

No service is ever restarted from here — a save writes the file (after a
timestamped .bak) and the page shows the `systemctl restart ...` command for
the operator to run. See the plan/docs for why (host systemd services and a
container deployment with no service manager at all).

These routers own only GET /config/beacon-{svxlink,direwolf} and
POST /config/beacon-{svxlink,direwolf}/conf. The catalog Save
(POST /config/{group}) and per-key reset stay on the generic handlers in
ui.routers.config — different method+path, no conflict — so this module must
be included BEFORE config.router (whose GET /config/{slug} catch-all would
otherwise win), mirroring how adapters.router sits before config.router.
"""
import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlencode

from adapters.storage import get_setting, record_audit_event
from fastapi import APIRouter, Depends, HTTPException, Request
from starlette.responses import RedirectResponse

from ..current_user import require_role
from ..db import get_db
from ..templating import templates
from .config import build_group_form_context

router = APIRouter(dependencies=[Depends(require_role("admin"))])

MAX_CONF_BYTES = 512 * 1024

# kind -> (group slug, path setting key, path default, systemd unit name)
_PAGES: dict[str, tuple[str, str, str, str]] = {
    "svxlink": ("beacon-svxlink", "BEACON_SVXLINK_CONF_PATH", "/etc/svxlink/svxlink.conf", "svxlink"),
    "direwolf": ("beacon-direwolf", "BEACON_DIREWOLF_CONF_PATH", "", "direwolf"),
}


def _editor_enabled(conn: sqlite3.Connection) -> bool:
    return get_setting(
        "BEACON_RF_CONF_EDITOR_ENABLED", "false", conn=conn
    ).lower() in ("true", "1", "yes", "on")


def _conf_path(conn: sqlite3.Connection, kind: str) -> str:
    _, key, default, _ = _PAGES[kind]
    return (get_setting(key, default, conn=conn) or "").strip()


def _read_conf(path: str) -> tuple[str | None, bool, str | None]:
    """(content, writable, error) for the conf file at `path`. content is None
    when it can't be shown; writable is whether a save would succeed (the file,
    or its parent dir when the file is absent)."""
    p = Path(path)
    try:
        if p.exists():
            if p.stat().st_size > MAX_CONF_BYTES:
                return None, False, f"{path} is larger than {MAX_CONF_BYTES // 1024} KiB — not editable here."
            content = p.read_text(encoding="utf-8")
            return content, os.access(p, os.W_OK), None
        parent = p.parent
        if not parent.is_dir():
            return None, False, f"{path} does not exist (and neither does {parent})."
        return "", os.access(parent, os.W_OK), f"{path} does not exist yet — saving will create it."
    except PermissionError:
        return None, False, f"Permission denied reading {path}."
    except UnicodeDecodeError:
        return None, False, f"{path} is not UTF-8 text — not editable here."
    except OSError as exc:
        return None, False, f"Could not read {path}: {exc}."


def _render(request: Request, conn: sqlite3.Connection, kind: str) -> object:
    group_slug, _, _, unit = _PAGES[kind]
    ctx = build_group_form_context(conn, group_slug)
    if ctx is None:  # group vanished from the catalog — shouldn't happen
        raise HTTPException(status_code=404, detail="settings group not found")

    enabled = _editor_enabled(conn)
    path = _conf_path(conn, kind)
    conf_content: str | None = None
    conf_writable = False
    conf_error: str | None = None
    if enabled and path:
        conf_content, conf_writable, conf_error = _read_conf(path)
    elif enabled and not path:
        conf_error = "No file path configured — set the path field above to enable the editor."

    ctx = dict(ctx)
    ctx.update(
        kind=kind,
        editor_enabled=enabled,
        conf_path=path,
        conf_content=conf_content,
        conf_writable=conf_writable,
        conf_error=conf_error,
        restart_cmd=f"sudo systemctl restart {unit}",
    )
    return templates.TemplateResponse(request, "config_rf_conf.html", ctx)


def _write_conf(conn: sqlite3.Connection, kind: str, content: str) -> str:
    """Write the conf file (timestamped .bak first, then an atomic replace).
    Returns a status message. Raises OSError on failure."""
    path = _conf_path(conn, kind)
    if not path:
        return "No file path configured — nothing saved."

    text = content.replace("\r\n", "\n")
    if text and not text.endswith("\n"):
        text += "\n"

    p = Path(path)
    backup_name: str | None = None
    if p.exists():
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        backup = p.with_name(f"{p.name}.{stamp}.bak")
        backup.write_bytes(p.read_bytes())
        backup_name = backup.name

    tmp = p.with_name(f".{p.name}.{os.getpid()}.part")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, p)

    record_audit_event(
        conn,
        event_type="beacon.rf_conf.edited",
        actor="ui.config",
        details={"kind": kind, "path": path, "bytes": len(text), "backup": backup_name},
    )
    unit = _PAGES[kind][3]
    saved = f"Saved {path}"
    if backup_name:
        saved += f" (backup: {backup_name})"
    return f"{saved}. Restart to apply: sudo systemctl restart {unit}"


def _make_get(kind: str):
    def _get(request: Request, conn: sqlite3.Connection = Depends(get_db)):
        return _render(request, conn, kind)

    return _get


def _make_post(kind: str):
    async def _post(request: Request, conn: sqlite3.Connection = Depends(get_db)):
        group_slug = _PAGES[kind][0]
        if not _editor_enabled(conn):
            raise HTTPException(status_code=404, detail="not found")
        form = await request.form()
        content = form.get("content")
        if not isinstance(content, str):
            content = ""
        if len(content.encode("utf-8")) > MAX_CONF_BYTES:
            msg = f"Too large — the editor caps files at {MAX_CONF_BYTES // 1024} KiB."
            return RedirectResponse(
                url=f"/config/{group_slug}?{urlencode({'error': msg})}", status_code=303
            )
        try:
            msg = _write_conf(conn, kind, content)
        except OSError as exc:
            return RedirectResponse(
                url=f"/config/{group_slug}?{urlencode({'error': f'Save failed: {exc}'})}",
                status_code=303,
            )
        return RedirectResponse(
            url=f"/config/{group_slug}?{urlencode({'msg': msg})}", status_code=303
        )

    return _post


for _kind, (_slug, *_rest) in _PAGES.items():
    router.add_api_route(f"/config/{_slug}", _make_get(_kind), methods=["GET"], include_in_schema=False)
    router.add_api_route(
        f"/config/{_slug}/conf", _make_post(_kind), methods=["POST"], include_in_schema=False
    )
