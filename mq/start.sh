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
# mosquitto_pub can hang on connect instead of failing fast — turning this
# into a silent multi-minute stall instead of a quick retry loop. The
# outer `timeout 3` bounds each attempt for the same reason: one hung
# attempt shouldn't be able to block this longer than the documented 60s.
until timeout 3 docker compose exec -T mosquitto mosquitto_pub -h 127.0.0.1 -t healthcheck -m ping -q 0 >/dev/null 2>&1; do
  attempts=$((attempts + 1))
  if [ "$attempts" -ge 60 ]; then
    echo "mosquitto did not become ready after 60s" >&2
    exit 1
  fi
  sleep 1
done

echo "mosquitto ready — mqtt://localhost:1883"
