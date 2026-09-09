#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"
source ../lib.sh

load_env_file ../.env
setup_venv

"$VENV_DIR/bin/python3" override_item.py "$@"
