#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"

ENV_FILE="../.env"
if [ -f "$ENV_FILE" ]; then
  # See adapters/start.sh for why this isn't a plain `source`.
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

MODEL="${ENRICHMENT_SUMARIZER_OLLAMA_MODEL:-llama3.2:1b}"

docker compose up -d

echo "waiting for the ollama server to accept connections..."
until docker compose exec -T ollama ollama list >/dev/null 2>&1; do
  sleep 1
done

# Idempotent: `ollama pull` is a no-op re-check if the model is already
# present locally, so this is safe to run on every start.
echo "pulling model: $MODEL (skipped if already present)"
docker compose exec -T ollama ollama pull "$MODEL"

echo "ollama ready — http://localhost:11434, model=$MODEL"
