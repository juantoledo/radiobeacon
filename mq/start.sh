#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"
source ../lib.sh

load_env_file ../.env

docker compose up -d

echo "waiting for mosquitto to accept connections..."
attempts=0
until docker compose exec -T mosquitto mosquitto_pub -h localhost -t healthcheck -m ping -q 0 >/dev/null 2>&1; do
  attempts=$((attempts + 1))
  if [ "$attempts" -ge 60 ]; then
    echo "mosquitto did not become ready after 60s" >&2
    exit 1
  fi
  sleep 1
done

echo "mosquitto ready — mqtt://localhost:1883"
