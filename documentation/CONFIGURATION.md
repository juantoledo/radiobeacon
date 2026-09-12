# Configuration

All configuration is a single `.env` file at the repo root (copy
`.env.example` to `.env`). Most settings are also editable live from the
dashboard's `/config` page. Variables specific to one component or source
are prefixed with its path (`ADAPTERS_SENAPRED_*`, `BEACON_*`); variables
read directly by a third-party SDK keep that SDK's own name.

## Station identity

The **station identity** (callsign, description, grid locator, frequency,
operator contact) is entered only in the dashboard and never read from the
environment — a stray `BEACON_CALLSIGN` in the shell can't put a wrong
callsign on air. The beacon refuses to transmit until every identity field
is filled in.

## Logging

All five services log to stderr in one shared format
(`adapters.logsetup`). The **`LOG_LEVEL`** setting (`DEBUG` \| `INFO` \|
`WARNING` \| `ERROR`, editable at `/config → Logging` or as an env var) is
picked up live — each service re-reads it once per loop tick, the UI
within 30 s — so no restart is needed to turn verbosity up or down.
`DEBUG` also un-mutes the HTTP and MQTT client libraries.

## Import / export config

The whole DB-backed config — settings, adapters, Policies, and source
names, secrets always excluded — can be snapshotted to a JSON file and
restored from one, from the dashboard
([`/config/import-export`](../ui/README.md#import--export-config-configimport-export))
or the command line
([`export_config.py`/`import_config.py`](../dispatcher/README.md#import--export-config)),
for backups or moving to a new host.

## Time is always UTC

Every datetime handled anywhere in this repo is timezone-aware UTC, with
no exceptions in storage, transmission scheduling, or internal
computation. The only place a local zone appears is text shown to a
person — spoken bulletin dates, the dashboard, human-readable logs — which
is converted to `DISPLAY_TIMEZONE` (default `America/Santiago`) at the
last step and never fed back into anything stored. See the code comments
in `adapters/timeutil.py` for the full rule.
