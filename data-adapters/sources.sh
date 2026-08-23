#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"
source ../lib.sh

load_env_file ../.env
setup_venv

.venv/bin/python3 sources.py "$@"
