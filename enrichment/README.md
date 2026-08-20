# enrichment

Summarizes items already stored in `storage/radiobeacon.db` via an LLM (Claude
or OpenAI) — genuinely condenses/rewrites the content, not extractive
sentence-selection. Fully decoupled from [adapters](../adapters/README.md):
adapters only fetch and store raw data; this package only reads/updates the
`summary` column of already-stored rows, run manually or by an
orchestrator, not as part of the fetch pipeline.

(Folder named `sumarizer`, not `summarizer` — deliberate.)

## Usage

```bash
./summarize_item.sh <item_id> [-n SENTENCES] [--db PATH]
```

Looks up one row by `item_id`, calls `summarize()`, writes the result back
to that row's `summary` column. `-n`/`--sentences` controls summary length
(default: 2). Creates a `.venv` and installs `requirements.txt` on first
run, same pattern as `adapters/start.sh`.

## `summarize()` (`src/enrichment/sumarizer/__init__.py`)

```python
summarize(fields: dict[str, Any], sentence_count: int = 2, provider: str | None = None) -> str | None
```

`fields` is a dict of column name → value — pass an items table row as-is
(e.g. `dict(cursor.fetchone())` with `sqlite3.Row`). Requires
`extracted_title` and/or `extracted_contents`; returns `None` if both are
missing/empty. `provider` defaults to the `ENRICHMENT_SUMARIZER_PROVIDER`
env var (`claude` or `openai`); falls back to the original combined text,
unchanged, if no provider is configured or the API call fails —
summarization should never hard-fail the caller.

Every key in `fields` becomes a `{key}` placeholder available to a custom
`ENRICHMENT_SUMARIZER_PROMPT` — in practice, any column of the items table
(`source`, `item_id`, `extracted_title`, `extracted_contents`, `url`,
`event_key`, `type`, `subtype`, `source_date_time`, `fetched_at`,
`captured_at`, `rawdata`, ...). Two extra placeholders are always added:
`{text}` (title + contents combined) and `{sentence_count}`.

## Configuration

Env vars, in `.env` at the repo root (see `.env.example`).

| var | meaning | default |
|---|---|---|
| `ENRICHMENT_SUMARIZER_PROVIDER` | `claude` or `openai`; unset disables summarization | unset |
| `ANTHROPIC_API_KEY` | required if provider is `claude` — read directly by the `anthropic` SDK | — |
| `OPENAI_API_KEY` | required if provider is `openai` — read directly by the `openai` SDK | — |
| `ENRICHMENT_SUMARIZER_CLAUDE_MODEL` | model used when provider is `claude` | `claude-haiku-4-5` |
| `ENRICHMENT_SUMARIZER_OPENAI_MODEL` | model used when provider is `openai` | `gpt-4o-mini` |
| `ENRICHMENT_SUMARIZER_PROMPT` | prompt template, `str.format`-substituted (escape literal `{`/`}` as `{{`/`}}`) | built-in Spanish default |

## Tests

```bash
.venv/bin/pytest tests/ -v
```
