#!/usr/bin/env bash
# Stops radiobeacon stack processes by PID file (see lib.sh's
# guard_single_instance/service_is_running, written to by every package's
# start.sh) -- works regardless of how or where a service was started:
# via the repo root's ./start.sh, a package's own start.sh run independently, or in a
# shell/session that's since ended. That last case is exactly the bug this
# exists to fix: a stale process from an earlier session with no record
# anywhere of it still running.
#
# Usage:
#   ./stop.sh                  stop every service + mq
#   ./stop.sh beacon actions   stop only the named service(s)
#   ./stop.sh --status         report running/stopped + pid, no killing
#
# Companion to ./start.sh (repo root) -- see there for a fresh-clone
# one-command way to start everything this stops.
set -uo pipefail   # not -e: keep going and report on every service even if one fails to stop
cd "$(dirname "$0")"
source ./lib.sh

# service_id -> module_name (see lib.sh's guard_single_instance calls in
# each start.sh -- data-adapters is the one place these differ).
SERVICE_IDS=(data-adapters dispatcher actions ui beacon)
module_name_for() {
  case "$1" in
    data-adapters) echo adapters ;;
    *) echo "$1" ;;
  esac
}

mq_is_running() {
  command -v docker >/dev/null 2>&1 || return 1
  [ -n "$(cd mq && docker compose ps -q mosquitto 2>/dev/null)" ]
}

print_status() {
  local name
  for name in "${SERVICE_IDS[@]}"; do
    if service_is_running "$name" "$(module_name_for "$name")"; then
      echo "$name: running (pid $(cat "$RUN_DIR/$name.pid"))"
    else
      echo "$name: not running"
    fi
  done
  if mq_is_running; then
    echo "mq: running"
  else
    echo "mq: not running"
  fi
}

stop_service() {
  local name="$1" module pid
  module="$(module_name_for "$name")"
  if ! service_is_running "$name" "$module"; then
    echo "$name: not running"
    rm -f "$RUN_DIR/$name.pid"
    return 0
  fi
  pid="$(cat "$RUN_DIR/$name.pid")"
  echo "$name: stopping pid $pid (SIGTERM)..."
  kill -TERM "$pid" 2>/dev/null || true
  local waited=0
  while [ "$waited" -lt 10 ] && service_is_running "$name" "$module"; do
    sleep 0.5
    waited=$((waited + 1))
  done
  if service_is_running "$name" "$module"; then
    echo "$name: still alive after SIGTERM, sending SIGKILL" >&2
    kill -KILL "$pid" 2>/dev/null || true
    sleep 0.5
  fi
  if service_is_running "$name" "$module"; then
    echo "$name: FAILED to stop pid $pid" >&2
    return 1
  fi
  echo "$name: stopped"
  rm -f "$RUN_DIR/$name.pid"
}

stop_mq() {
  if ! command -v docker >/dev/null 2>&1; then
    return 0
  fi
  if ! mq_is_running; then
    echo "mq: not running"
    return 0
  fi
  echo "mq: stopping (docker compose down)..."
  ( cd mq && docker compose down ) && echo "mq: stopped"
}

if [ "${1:-}" = "--status" ]; then
  print_status
  exit 0
fi

targets=("$@")
include_mq=1
if [ ${#targets[@]} -eq 0 ]; then
  targets=("${SERVICE_IDS[@]}")
else
  include_mq=0
fi

status=0
for name in "${targets[@]}"; do
  if [ "$name" = "mq" ]; then
    stop_mq || status=1
    continue
  fi
  stop_service "$name" || status=1
done
if [ "$include_mq" -eq 1 ]; then
  stop_mq || status=1
fi

exit "$status"
