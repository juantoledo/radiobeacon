#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"

ENV_FILE="../.env"
if [ -f "$ENV_FILE" ]; then
  # Parsed line-by-line instead of `source`d: a plain `source .env` runs
  # each line through bash's normal command parsing, so any unquoted value
  # containing spaces (e.g. ENRICHMENT_SUMARIZER_PROMPT=Summarize this...) gets
  # misread as a command invocation and aborts the script. This loop just
  # splits each line on the first `=` and exports it directly, no
  # command parsing involved.
  while IFS= read -r line || [ -n "$line" ]; do
    case "$line" in
      ''|'#'*) continue ;;
    esac
    key="${line%%=*}"
    value="${line#*=}"
    if [[ "$value" == \"*\" && "$value" == *\" ]]; then
      value="${value#\"}"
      value="${value%\"}"
    elif [[ "$value" == \'*\' && "$value" == *\' ]]; then
      value="${value#\'}"
      value="${value%\'}"
    fi
    export "$key=$value"
  done < "$ENV_FILE"
fi

if [ ! -d .venv ]; then
  python3 -m venv .venv
fi

.venv/bin/pip install -q -r requirements.txt
PYTHONPATH=src .venv/bin/python3 -m adapters
