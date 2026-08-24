#!/usr/bin/env bash
# One command for a fresh clone: creates .env from .env.example if
# missing, sets up every Python package's .venv, brings up the local MQTT
# broker, then starts data-adapters, dispatcher, actions, ui, and beacon
# together — tearing all of it down cleanly on Ctrl+C/SIGTERM. Every step
# here is idempotent, so re-running is safe. To run just one piece
# instead, use that package's own start.sh directly. beacon starts
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

for pkg in data-adapters dispatcher actions ui beacon; do
  echo "== $pkg: setting up .venv =="
  ( cd "$pkg" && setup_venv )
done

if command -v docker >/dev/null 2>&1; then
  echo "== mq: starting broker =="
  ./mq/start.sh
else
  echo "== mq: docker not found, skipping broker — actions will retry connecting until it's up =="
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
