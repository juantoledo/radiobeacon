# ollama

Runs a self-hosted [Ollama](https://ollama.com) server in Docker — a local,
no-API-key alternative to Claude/OpenAI for
[enrichment](../enrichment/README.md)'s summarizer, meant for running the
whole pipeline offline on constrained hardware (e.g. a ZimaBoard).

## Usage

```bash
./start.sh
```

Brings up the container (`docker compose up -d`) and pulls
`ENRICHMENT_SUMARIZER_OLLAMA_MODEL` (default `llama3.2:1b`) into it — reads
the same repo-root `.env` as every other package. Re-running is safe: `ollama
pull` no-ops if the model is already present. The server listens on
`localhost:11434` only (see `docker-compose.yml`) — not exposed to the
network by default.

Then point enrichment at it:

```bash
ENRICHMENT_SUMARIZER_PROVIDER=ollama ./enrichment/summarize_item.sh <item_id>
```

## Choosing a model

Ollama has no GPU to lean on here — inference is CPU-only, so pick a model
size that fits both the board's RAM and its patience. Note that Ollama's
default tag for a model isn't always the same quantization — `llama3.2:1b`
and `smollm2:360m` default to Q8_0/F16, not Q4, so disk/RAM size doesn't
scale with parameter count the way you'd expect. Measured by actually
running this repo's Spanish summarization prompt against each (not spec-sheet
numbers):

| Model | Disk | Resident RAM | Latency | Result |
|---|---|---|---|---|
| `llama3.2:1b` (default) | 1.3GB (Q8_0) | 1.8GB | ~5s | Correct, coherent, a little wordier |
| `qwen2.5:1.5b` | 986MB (Q4_K_M) | 1.4GB | ~3s | Correct, occasional odd word choice |
| `qwen2.5:0.5b` | 397MB (Q4_K_M) | 628MB | <1s | Correct and faithful — best result of the four, if you need to fit 2GB RAM |
| `smollm2:360m` | 725MB (F16) | 919MB | ~2s | **Not recommended** — in testing it just echoed the title back instead of summarizing; its instruction-following is weak enough in Spanish that it's not worth the RAM it uses |

Change `ENRICHMENT_SUMARIZER_OLLAMA_MODEL` in `.env`, then re-run
`./start.sh` to pull the new one (the old one stays cached in the
`ollama-models` volume until removed manually).

Expect rougher summaries than Claude Haiku at this scale — these models are
small enough to occasionally miss nuance or repeat themselves. That's the
tradeoff for running fully offline with no per-call cost.

## Deploying on a ZimaBoard

ZimaBoard is x86_64, so the standard `ollama/ollama` Docker image runs
as-is — no ARM-specific image or cross-compilation concerns, unlike a
Raspberry Pi. Docker + Docker Compose need to already be installed on the
board; everything else here is identical to running it on any other Linux
host.

## Stopping / removing

```bash
docker compose down          # stop the container, keep the model volume
docker compose down -v       # also delete downloaded models
```
