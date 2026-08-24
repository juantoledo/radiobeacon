#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"
source ../lib.sh

load_env_file ../.env
setup_venv

# exec replaces this shell process with the Python one so SIGINT/SIGTERM
# sent to this script reach uvicorn directly — see data-adapters/start.sh
# for the same pattern.
export PYTHONPATH=src
guard_single_instance ui ui
exec .venv/bin/python3 -m ui
