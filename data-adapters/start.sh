#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"
source ../lib.sh

load_env_file ../.env
setup_venv

# exec replaces this shell process with the Python one (instead of running
# it as a child) so SIGINT/SIGTERM sent to this script reach the
# long-running adapter loop directly — see data-adapters/src/adapters/__main__.py.
export PYTHONPATH=src
guard_single_instance data-adapters adapters
exec .venv/bin/python3 -m adapters
