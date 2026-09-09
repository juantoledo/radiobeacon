#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"
source ../lib.sh

load_env_file ../.env

docker compose up -d

echo "waiting for mosquitto to accept connections..."
attempts=0
# 127.0.0.1, not localhost: some hosts resolve localhost to ::1 first, and
# if IPv6 loopback doesn't route cleanly in the container's network stack,
# mosquitto_pub can hang on connect instead of failing fast.
#
# </dev/null is load-bearing, not cosmetic: `-T` only disables pseudo-tty
# allocation, it does NOT stop `docker compose exec` from attaching stdin.
# Run this script from a real interactive terminal and that attached
# stdin can trigger SIGTTIN — the kernel suspends (not kills) the process
# the moment it tries to read from the controlling tty while outside the
# shell's foreground process group, which `until`/`run_with_timeout`
# nesting here reliably produces. A SIGTTIN-stopped process ignores
# SIGINT *and* SIGTERM (only SIGCONT/SIGKILL affect it), so without this
# redirect a single attempt can wedge forever, immune to both this loop's
# own timeout and the user's own Ctrl+C. run_with_timeout's 2s kill grace
# period is defense in depth: if a future change ever reintroduces a hang
# some other way, force-kill 2s after the TERM it sends on its own.
#
# run_with_timeout (lib.sh), not GNU `timeout`, since macOS doesn't ship it.
until run_with_timeout 3 docker compose exec -T mosquitto mosquitto_pub -h 127.0.0.1 -t healthcheck -m ping -q 0 </dev/null >/dev/null 2>&1; do
  attempts=$((attempts + 1))
  if [ "$attempts" -ge 60 ]; then
    echo "mosquitto did not become ready after 60s" >&2
    exit 1
  fi
  sleep 1
done

echo "mosquitto ready — mqtt://localhost:1883"
