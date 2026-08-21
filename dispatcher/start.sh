#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"
source ../lib.sh

load_env_file ../.env
setup_venv

# exec replaces this shell process with the Python one so SIGINT/SIGTERM
# sent to this script reach the long-running poll loop directly — see
# data-adapters/start.sh for the same pattern.
export PYTHONPATH=src
exec .venv/bin/python3 -m dispatcher
