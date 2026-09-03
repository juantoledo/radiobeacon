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
| `/dev` | Developers: raw item add/edit/delete, dispatcher-state reset, read-only SQL runner — see below |

Every mutating action (override, rearm, policy create/edit/delete) is a
plain HTML form POST, redirecting back to a GET page afterward. The only
client-side JavaScript in the app is the dashboard's auto-refresh (below)
and a confirm() prompt on Developers' delete buttons — every other page
is plain server-rendered HTML with zero JS.

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

## No authentication

This first version has no login system — it's meant to be reached only
from `localhost` or a trusted network. `ui/start.sh` binds `0.0.0.0` by
default (all interfaces, reachable from the LAN, e.g.
`192.168.200.145:8080`); set `UI_HOST=127.0.0.1` to keep it loopback-only
instead. Since it can trigger write actions against
`storage/radiobeacon.db`, don't expose this beyond a trusted network
without adding auth first — this goes double for `/dev` (raw item add/
edit/delete), which you may also want to disable outright via
`UI_DEV_TOOLS_ENABLED=false` on any deployment where you don't need it.

## Usage

```bash
./start.sh
```

Same pattern as every other package's `start.sh`: creates a `.venv`,
installs `requirements.txt`, loads `../.env`, then `exec`s into Uvicorn
(`PYTHONPATH=src python3 -m ui`). Visit `http://127.0.0.1:8080` (or
whatever LAN address it's reachable at — see "No authentication" above).
`Ctrl+C`/`SIGTERM` stops it cleanly.

## Configuration

Env vars, in `.env` at the repo root (see `.env.example`).

| var | default |
|---|---|
| `UI_HOST` | `0.0.0.0` |
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

Built from the **repo root**, not `ui/` alone — `ui/src/ui/app.py`'s
`sys.path` bootstrap expects `data-adapters/src` and `dispatcher/src` as
siblings on disk, same as `dispatcher/__main__.py`/`override_item.py` rely
on outside Docker, so the image preserves that layout.

```bash
docker compose -f ui/docker-compose.yml up --build
```

or, without Compose:

```bash
docker build -f ui/Dockerfile -t radiobeacon-ui .
docker run --rm -p 8080:8080 \
  -v "$(pwd)/storage:/app/storage" \
  -e UI_HOST=0.0.0.0 -e UI_DB_PATH=/app/storage/radiobeacon.db \
  radiobeacon-ui
```

Use `-p 127.0.0.1:8080:8080` instead if you want it reachable only from
`localhost`, not the LAN.

The `storage/` directory is bind-mounted read-write (not a named volume,
and not `:ro`) — the UI writes to `radiobeacon.db` via its override/rearm/
policy actions, and this is the same file every other process (Dockerized
or not) reads and writes, so it has to be the real one on disk, not a
container-private copy. `UI_HOST=0.0.0.0` is required for the app to be
reachable through the container's port mapping — `127.0.0.1` inside the
container is unreachable from outside it; the `ports:`/`-p` binding is
what actually controls host-side access (loopback-only vs. all
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
