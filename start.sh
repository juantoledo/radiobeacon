#!/usr/bin/env bash
# One command for a fresh clone: creates .env from .env.example if
# missing, sets up one shared .venv for every Python package (see
# lib.sh's VENV_DIR), brings up the local MQTT broker, then starts
# data-adapters, dispatcher, actions, ui, and beacon together — tearing
# all of it down cleanly on Ctrl+C/SIGTERM. Every step here is idempotent,
# so re-running is safe. To run just one piece instead, use that
# package's own start.sh directly — it installs into the same shared
# venv. beacon starts
# disabled (BEACON_ENABLED=false) and — even once enabled — only its
# hardware-independent parts (queuing, scheduling, the default logging-only
# transmitters) do anything without a real SvxLink/Direwolf/radio present;
# see beacon/README.md.
set -euo pipefail
cd "$(dirname "$0")"
source ./lib.sh

if [ -f .env ]; then
  echo ".env already exists, leaving it as-is"
elif [ -f .env.example ]; then
  cp .env.example .env
  echo "created .env from .env.example — every default is safe to run as-is (no secrets required)"
else
  echo "warning: .env.example not found, skipping .env creation" >&2
fi

# mq/start.sh's own docker-compose-up + readiness poll is independent of
# venv setup below (it only needs Docker, not the venv), so it's kicked
# off in the background here and waited on further down — overlapping its
# latency with pip's instead of paying for both in sequence. Runs in the
# background, not backgrounded via setup_venv_all's subshell trick,
# because setup_venv_all itself must stay in this shell (not a subshell)
# for its RADIOBEACON_VENV_READY export (see lib.sh) to reach the five
# services backgrounded further below.
mq_pid=""
if command -v docker >/dev/null 2>&1; then
  echo "== mq: starting broker (overlapped with venv setup below) =="
  ./mq/start.sh &
  mq_pid=$!
else
  echo "== mq: docker not found, skipping broker — actions will retry connecting until it's up =="
fi

echo "== setting up shared .venv for data-adapters, dispatcher, actions, ui, beacon =="
setup_venv_all data-adapters/requirements.txt dispatcher/requirements.txt \
  actions/requirements.txt ui/requirements.txt beacon/requirements.txt

if [ -n "$mq_pid" ]; then
  wait "$mq_pid"
fi

# data-adapters/dispatcher/actions/ui/beacon each exec into a long-running
# foreground process (see their own start.sh) — run them in the
# background here and wait, so Ctrl+C to this script (not each of theirs)
# tears down all five together instead of leaving orphans behind. Each of
# them writes its own PID file via guard_single_instance (lib.sh) on the
# way in, so cleanup below just delegates to ./stop.sh — the same stop
# path ./stop.sh gives you manually from any other shell, rather than
# duplicating kill logic here too.
cleanup_done=0
cleanup() {
  [ "$cleanup_done" -eq 1 ] && return
  cleanup_done=1
  echo
  echo "shutting down..."
  ./stop.sh
}
trap cleanup EXIT INT TERM

echo "== starting data-adapters, dispatcher, actions, ui, beacon (Ctrl+C to stop all) =="
./data-adapters/start.sh &
./dispatcher/start.sh &
./actions/start.sh &
./ui/start.sh &
./beacon/start.sh &

wait
