# ui

A server-rendered ops dashboard for `storage/radiobeacon.db` — browse and
search items, override an item's `transmit_policy`, re-arm a retired item
for redelivery, manage `transmit_policies`, and view the `audit_log`.
FastAPI + Jinja2 (plain server-rendered HTML, no JS framework) + Uvicorn.
The first HTTP-serving code in this repo.

Fully decoupled the same way [dispatcher](../dispatcher/README.md) is from
[data-adapters](../data-adapters/README.md): it only reads/writes
`storage/radiobeacon.db`, wired to the rest of the pipeline only through
that shared file. Item overrides and rearms go through the exact same
functions `dispatcher/override_item.py` uses
(`dispatcher.override.override_item`/`rearm_item`), and policy management
goes through the same functions `dispatcher/policies.py` uses
(`adapters.transmit_policy.set_policy`/`delete_policy`) — so an action taken here
is indistinguishable, from the rest of the system's point of view, from
one taken via those CLIs (same `audit_log` events, same optional MQTT
publish).

## Pages

| Page | Purpose |
|---|---|
| `/` | Dashboard: item/audit/policy counts, recent items, recent audit events |
| `/items` | Browse/search items — filter by source, type, transmit_policy, event_key, or free-text search |
| `/items/{source}/{item_id}` | Item detail: full contents, chunks, per-item audit trail, override + rearm forms |
| `/policies` | List/create/edit/delete named `transmit_policies` |
| `/audit` | Global audit log, filterable by event_type/source/item_id |
| `/config/import-export` | Export the whole DB-backed config (settings, adapters, policies, source names) as one JSON file, or import one — see below |
| `/dev` | Developers: raw item add/edit/delete, dispatcher-state reset, read-only SQL runner — see below |

Every mutating action (override, rearm, policy create/edit/delete) is a
plain HTML form POST, redirecting back to a GET page afterward. The only
client-side JavaScript in the app is the dashboard's auto-refresh (below)
and a confirm() prompt on Developers' delete buttons — every other page
is plain server-rendered HTML with zero JS.

## Import / Export config (`/config/import-export`)

A whole-DB config snapshot as one JSON file — settings overrides, transmit
policies, source display names, and every adapter definition (bundled with
its source row) — going through the exact same functions the CLI pair
(`dispatcher/export_config.py` / `import_config.py`) and every other manual
edit already use (`adapters.config_transfer`, shared with those scripts),
so an import is indistinguishable from the same edits made by hand.

- **Secrets are always excluded from export** — `ANTHROPIC_API_KEY`/
  `OPENAI_API_KEY` never appear in the file, not even as a placeholder key.
- **Import is merge/upsert-only** — a record overwrites an existing one
  sharing its name/source, but nothing already on this install that's
  absent from the file is ever deleted.
- Uploading a file previews what would change (created/updated/skipped,
  with reasons) before anything is written; confirming applies it.
- A CUSTOM-type adapter (operator-authored Python, `exec`'d with no
  sandboxing) only imports when this install's `UI_DEV_TOOLS_ENABLED` is
  on — same gate `/config/adapters` already applies to creating/editing
  one — and its code is shown for review, never executed or test-run
  during import either way.

## Developers section (`/dev`)

A deliberately separate, more dangerous surface than the rest of the app
— everywhere else, `items` is treated as immutable after insert except
`summary`/`transmit_policy` (see
`adapters.storage.store_reading`'s docstring); `/dev` is an explicit,
clearly-labeled escape hatch around that for debugging/backfilling, not a
replacement for the normal adapter → dispatcher pipeline.

- **Add/edit/delete items** (`/dev/items/new`, `/dev/items/{source}/{item_id}/edit`)
  — every field except the primary key (`source`, `item_id`, fixed after
  creation — changing it would orphan the item's existing
  chunks/audit_log/trigger_dispatches rows) and the write-once bookkeeping
  columns (`fetched_at`, `captured_at`, `rawdata`). Deleting an item also
  deletes its `chunks` and `trigger_dispatches`/`item_policy_state` rows
  (operational, meaningless once the item is gone); `audit_log` rows are
  kept as history, and the deletion itself is recorded as one more
  `audit_log` event. Every write here uses actor `ui.dev` in `audit_log`
  (`item.created`/`item.dev_edited`/`item.deleted`) so it's never
  confused with a normal adapter/dispatcher/override write.
- **Reset dispatcher state** (on the edit page) — clears a consumer's
  `trigger_dispatches`/`item_policy_state` rows for one item *without*
  re-arming it (unlike Rearm on the item's own `/items/...` page, which
  schedules an immediate redelivery). For clearing stuck/incorrect
  bookkeeping while debugging. Implemented in
  `dispatcher.override.reset_dispatch_state`, same module as
  `override_item`/`rearm_item`.
- **SQL runner** (`/dev/sql`) — ad hoc, read-only queries; only a single
  `SELECT` (or `WITH ... SELECT`) statement is accepted
  (`ui.sql_guard.ensure_select_only`), and results are capped at 500 rows.
  The actual safety boundary isn't that validation, though — it's that
  the runner opens `storage/radiobeacon.db` via SQLite's own read-only
  URI mode (`ui.db.open_readonly_connection`, `mode=ro`), so even a query
  that somehow slipped past validation can't write anything; SQLite
  refuses at the driver level.

Set `UI_DEV_TOOLS_ENABLED=false` to disable this entire section (every
`/dev/*` route 404s, and the nav link disappears) without disabling the
rest of the UI.

## Live dashboard

`/` polls itself every `UI_DASHBOARD_REFRESH_SECONDS` (default 5) and
swaps in the freshly-rendered content in place — no full page reload, no
manual refresh needed to see a new item or audit event land. This is
polling, not a push/subscription: there's no cross-process change
notification to hook into (data-adapters/dispatcher write
`storage/radiobeacon.db` from their own separate processes and
connections), so the browser re-asking the server "what does this look
like now" on an interval is the same mechanism every other consumer of
that file already uses, just done client-side
(`ui/src/ui/static/dashboard-refresh.js` — plain `fetch`/`DOMParser`, no
framework, no build step). Paused while the tab is backgrounded, resumed
immediately on refocus. Set `UI_DASHBOARD_REFRESH_SECONDS=0` to disable
entirely.

The dashboard's own "Beacon" section (enable/disable, beacon type,
pending-transmit count, last-transmit time) refreshes on that same
interval — it's part of the same `#dashboard-content` block, not a
separate page. The values come from `beacon_status` (`beacon_type`, the
active type's `*_queue_depth`, `last_{voice,frame}_transmit_at`), written
every tick by `beacon/src/beacon/__main__.py::_write_heartbeat`. The
"running" badge tracks whether `process_heartbeat_at` is recent.

### Play a bulletin's audio

Rows in the dashboard's Activity → Items feed show a small play button
when the beacon has already rendered a voice clip for that item. It
streams the WAV from `GET /items/{source}/{item_id}/audio` and plays it
through one shared `<audio>` element (`ui/src/ui/static/beacon-audio.js`),
so you can hear exactly what went on air without a radio. Only spoken
(voice) clips are offered — frame/packet clips are AFSK modem tones, not
speech.

The clips come straight from `BEACON_TTS_WAV_DIR` (`ui.beacon_audio`), the
directory the beacon writes into. A relative value there is resolved
against the repo root by both processes, so the default `storage/beacon_tts`
is the top-level `storage/` the `ui/` container already bind-mounts. In a
split deployment (beacon on the host, UI in Docker) point it at a path
both can see, or no button appears.

### Transmit now (manual message)

The **Transmit text now** button in Quick controls opens a modal to send a
one-shot message: pick voice or frame, type the text (live character/byte
counter, blocked past `BEACON_VOICE_MAX_CHARS` for voice or the AX.25 frame
size for frame), hit Transmit. `POST /dashboard/transmit`
(`ui/src/ui/routers/manual_tx.py`) validates and writes a row to
`beacon_manual_tx`; the beacon renders and keys it on its next tick
(`beacon.__main__._drain_manual_tx`). No TTS runs in the UI — it only
writes DB state, same as every other action here.

It's sent once and dropped — no repeat, no retry. Like the automatic
beacon it only goes on air while `BEACON_ENABLED` is on; composed while
it's off, the message queues and the modal says so. A message composed
for the kind the beacon isn't currently in (`BEACON_TYPE`) waits until you
switch mode. The callsign is always added (voice via
`BEACON_MANUAL_VOICE_TEMPLATE`, frame via the AX.25 header). The modal
needs JavaScript; the button is inert without it.

Once the beacon has rendered a manual message, a **Recent manual
transmissions** list appears under the button (last ~5, newest first) with
a play button per clip — same shared player as the bulletin audio, served
from `GET /dashboard/manual-audio/{name}` (`manual-<id>-<ts>.wav` files in
`BEACON_TTS_WAV_DIR`). Both voice and frame manual clips are listed; a
frame one plays as AFSK tones.

## Authentication

Login is required for every page except `/login` itself. Two roles:

- **admin** — full access to everything (dashboard, items, config,
  adapters, policies, audit log, `/dev`, and user management at
  `/users`).
- **user** — read-only access to the dashboard only; every other route
  (including `/items`, `/config`, `/audit`, `/dev`, manual transmit)
  answers 403.

An `admin` account is created automatically the first time the app
starts against a database with no admin user yet — its username
(`admin`) and a randomly generated password are printed once to the
server log (look for `===== INITIAL ADMIN PASSWORD =====`). Log in and
change that password immediately, either from `/users` or with the
recovery script below (which also works if you're locked out and can't
log in at all):

```bash
../data-adapters/manage_users.sh set-password admin
../data-adapters/manage_users.sh list
../data-adapters/manage_users.sh add-user someone --role user
../data-adapters/manage_users.sh set-role someone --role admin
../data-adapters/manage_users.sh delete-user someone
```

Sessions are DB-backed (a `sessions` table, not a JWT), stored as an
httponly cookie, and last 30 days on a sliding window (refreshed once
more than half spent). Changing a user's password or role, or deleting
them, immediately invalidates their existing sessions.

This is still application-level auth, not network isolation — `UI_HOST`
defaults to `0.0.0.0` (all interfaces) and `UI_ALLOWED_HOSTS` defaults to
`*` (the DNS-rebinding/cross-origin guard off). For defense in depth
beyond the login itself, narrow `UI_ALLOWED_HOSTS` to the exact names/IPs
you serve under (see `ui/src/ui/security.py`), or set `UI_HOST=127.0.0.1`
and put a reverse proxy in front. `/dev` and CUSTOM adapters (arbitrary
SQL / `exec`) are admin-only on top of that, and can still be disabled
outright with `UI_DEV_TOOLS_ENABLED=false`.

## Usage

```bash
./start.sh
```

Same pattern as every other package's `start.sh`: creates a `.venv`,
installs `requirements.txt`, loads `../.env`, then `exec`s into Uvicorn
(`PYTHONPATH=src python3 -m ui`). Visit `http://<host>:8080` — it binds
all interfaces by default (see "Authentication" above for the
auto-created admin account and login).
`Ctrl+C`/`SIGTERM` stops it cleanly.

## Configuration

Env vars, in `.env` at the repo root (see `.env.example`).

| var | default |
|---|---|
| `UI_HOST` | `0.0.0.0` *(all interfaces)* |
| `UI_ALLOWED_HOSTS` | `*` *(Host/cross-origin guard off)* |
| `UI_PORT` | `8080` |
| `UI_DB_PATH` | *(unset — uses `adapters.storage.DEFAULT_DB_PATH`, `storage/radiobeacon.db`)* |
| `UI_PAGE_SIZE` | `50` |
| `UI_DEFAULT_CONSUMER_NAME` | *(unset — falls back to `DISPATCHER_CONSUMER_NAME`, then `"log"`)* |
| `UI_DASHBOARD_REFRESH_SECONDS` | `5` |
| `UI_DEV_TOOLS_ENABLED` | `true` |

Optional MQTT publishing of override/rearm/policy-set/policy-deleted audit
events is controlled by `DISPATCHER_MQ_HOST` (and the other
`DISPATCHER_MQ_*` vars) — see [dispatcher/README.md](../dispatcher/README.md#publishing-to-a-message-queue-cloudevents-over-mqtt).

## Running in Docker

Built from the **repo root**, not `ui/` alone — `ui/requirements.txt`
installs the sibling `data-adapters/` and `dispatcher/` packages editable
(`-e ../data-adapters`, `-e ../dispatcher`), so the image copies all three
in under `/app` and runs `pip install` from `/app/ui`.

```bash
docker compose -f ui/docker-compose.yml up --build
```

or, without Compose:

```bash
docker build -f ui/Dockerfile -t radiobeacon-ui .
docker run --rm -p 8080:8080 \
  -v "$(pwd)/storage:/app/storage" \
  -e UI_DB_PATH=/app/storage/radiobeacon.db \
  radiobeacon-ui
```

`UI_HOST` already defaults to `0.0.0.0` and `UI_ALLOWED_HOSTS` to `*`, so
no host env vars are needed. Use `-p 127.0.0.1:8080:8080` if you want it
reachable only from `localhost`, not the LAN. To re-arm the Host guard,
set `UI_ALLOWED_HOSTS` to whatever name you open the dashboard under
(e.g. the mini-PC's LAN IP or `.local` name) — then a `Host` header that
isn't listed gets a 400.

The `storage/` directory is bind-mounted read-write (not a named volume,
and not `:ro`) — the UI writes to `radiobeacon.db` via its override/rearm/
policy actions, and this is the same file every other process (Dockerized
or not) reads and writes, so it has to be the real one on disk, not a
container-private copy. The default `UI_HOST=0.0.0.0` is what makes the
app reachable through the container's port mapping — `127.0.0.1` inside
the container is unreachable from outside it; the `ports:`/`-p` binding
is what actually controls host-side access (loopback-only vs. all
interfaces).

## Tests

```bash
.venv/bin/pytest tests/ -v
```

`tests/test_queries.py` covers `ui/src/ui/queries.py`'s read helpers, and
`tests/test_dev_ops.py` covers `ui/src/ui/dev_ops.py`'s writes (create/
update/delete + their cascade/audit behavior), both against a real,
fully-migrated schema (`adapters.storage.get_connection` +
`dispatcher.watcher._ensure_tables`, not a hand-rolled stand-in).
`tests/test_sql_guard.py` covers the SQL runner's query validation.
`tests/test_routes.py` and `tests/test_dev_routes.py` cover the FastAPI
routes with `TestClient` — dependency wiring, redirect-after-POST
behavior, 404s, form validation, and (for `/dev/sql`) that a write
statement is rejected both by validation and, independently, by the
read-only connection itself — not exhaustive HTML assertions.
