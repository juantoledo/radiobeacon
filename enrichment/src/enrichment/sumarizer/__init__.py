import logging
import os
from typing import Any

logger = logging.getLogger(__name__)

PROVIDER_ENV_VAR = "ENRICHMENT_SUMARIZER_PROVIDER"
CLAUDE_MODEL = os.environ.get("ENRICHMENT_SUMARIZER_CLAUDE_MODEL", "claude-haiku-4-5")
OPENAI_MODEL = os.environ.get("ENRICHMENT_SUMARIZER_OPENAI_MODEL", "gpt-4o-mini")
OLLAMA_MODEL = os.environ.get("ENRICHMENT_SUMARIZER_OLLAMA_MODEL", "llama3.2:1b")
OLLAMA_HOST = os.environ.get("ENRICHMENT_SUMARIZER_OLLAMA_HOST", "http://localhost:11434")

# Hard cap on the returned summary length, regardless of provider or of
# what the prompt itself asks for. Needed because asking an LLM to self-
# enforce a character count is unreliable — verified empirically: even a
# prompt that explicitly says "count the characters, shorten if over" let
# a small local model (llama3.2:1b) blow past a 200-char budget on a long
# input. Applies uniformly to every return path, including the
# no-provider-configured and API-failure fallbacks, since the underlying
# constraint (e.g. an AX.25 UI frame's ~256-byte payload, see CONTEXT.md)
# doesn't care which path produced the text. Unset by default — off unless
# a caller opts in.
_MAX_CHARS_RAW = os.environ.get("ENRICHMENT_SUMARIZER_MAX_CHARS")
MAX_CHARS = int(_MAX_CHARS_RAW) if _MAX_CHARS_RAW else None

DEFAULT_PROMPT_TEMPLATE = (
    "Resume el siguiente texto en español en exactamente {sentence_count} "
    "oración(es), de forma concisa y fiel al contenido original. "
    "Responde solo con el resumen, sin comentarios adicionales.\n\n"
    "Texto:\n{text}"
)
# Overridable via ENRICHMENT_SUMARIZER_PROMPT. Substituted via str.format — any other
# literal `{`/`}` in a custom prompt needs to be escaped as `{{`/`}}`.
#
# Placeholders: every key in the `fields` dict passed to summarize() is
# available by name — in practice, every column of the items table (source,
# item_id, extracted_title, extracted_contents, url, source_date_time,
# fetched_at, captured_at, rawdata, ...), since callers pass the row as-is.
# Missing/None values render as "". Two extra placeholders are always added
# on top of `fields`: {text} (extracted_title + extracted_contents combined)
# and {sentence_count} (the requested summary length).
PROMPT_TEMPLATE = os.environ.get("ENRICHMENT_SUMARIZER_PROMPT", DEFAULT_PROMPT_TEMPLATE)


def _combine_text(extracted_title: str | None, extracted_contents: str | None) -> str | None:
    parts = [part for part in (extracted_title, extracted_contents) if part]
    return "\n\n".join(parts) if parts else None


def _truncate(result: str, max_chars: int | None) -> str:
    if max_chars is None or len(result) <= max_chars:
        return result
    truncated = result[:max_chars]
    # Back off to the last word boundary so a hard cutoff doesn't sever a
    # word mid-way — only if there is one in the truncated slice at all.
    last_space = truncated.rfind(" ")
    if last_space > 0:
        truncated = truncated[:last_space]
    return truncated.rstrip()


def _prompt(fields: dict[str, Any], text: str, sentence_count: int) -> str:
    format_kwargs = {k: ("" if v is None else v) for k, v in fields.items()}
    format_kwargs["text"] = text
    format_kwargs["sentence_count"] = sentence_count
    return PROMPT_TEMPLATE.format(**format_kwargs)


def _summarize_claude(prompt: str) -> str:
    from anthropic import Anthropic

    client = Anthropic()
    response = client.messages.create(
        model=CLAUDE_MODEL,
        max_tokens=512,
        messages=[{"role": "user", "content": prompt}],
    )
    return response.content[0].text.strip()


def _summarize_openai(prompt: str) -> str:
    from openai import OpenAI

    client = OpenAI()
    response = client.chat.completions.create(
        model=OPENAI_MODEL,
        max_tokens=512,
        messages=[{"role": "user", "content": prompt}],
    )
    return response.choices[0].message.content.strip()


def _summarize_ollama(prompt: str) -> str:
    from ollama import Client

    client = Client(host=OLLAMA_HOST)
    response = client.chat(
        model=OLLAMA_MODEL,
        messages=[{"role": "user", "content": prompt}],
    )
    return response["message"]["content"].strip()


_VALID_PROVIDERS = ("claude", "openai", "ollama")
_MODEL_BY_PROVIDER = {"claude": CLAUDE_MODEL, "openai": OPENAI_MODEL, "ollama": OLLAMA_MODEL}


def summarize(
    fields: dict[str, Any],
    sentence_count: int = 2,
    provider: str | None = None,
) -> str | None:
    """Abstractive summary via an LLM API (Claude, OpenAI, or a self-hosted
    Ollama server) — genuinely condenses/rewrites, unlike extractive methods
    that only select existing sentences.

    `fields` is a dict of column name -> value — pass the items table row
    as-is (e.g. `dict(cursor.fetchone())` with `sqlite3.Row`, or the field
    values you're about to insert for a not-yet-stored row). Every key
    becomes a `{key}` placeholder a custom ENRICHMENT_SUMARIZER_PROMPT can reference,
    so any current or future column works without changing this function —
    see PROMPT_TEMPLATE above for the full placeholder contract. Must
    contain `extracted_title` and/or `extracted_contents` — returns None if
    both are missing/empty (nothing to summarize).

    `provider` defaults to the ENRICHMENT_SUMARIZER_PROVIDER env var ("claude",
    "openai", or "ollama"). API keys for claude/openai are read by each SDK
    from its own standard env var (ANTHROPIC_API_KEY / OPENAI_API_KEY) — not
    handled here. ollama needs no key; it talks to a local/self-hosted
    server at ENRICHMENT_SUMARIZER_OLLAMA_HOST instead.

    Falls back to the combined text, unchanged, if no provider is
    configured/recognized or the API call fails (network, auth, rate
    limit, etc.) — summarization should never break the fetch pipeline.

    Every return path (success, no-provider fallback, and failure fallback)
    is capped at ENRICHMENT_SUMARIZER_MAX_CHARS if set — see MAX_CHARS above
    for why this can't just be left to the prompt."""
    text = _combine_text(fields.get("extracted_title"), fields.get("extracted_contents"))
    if not text:
        return None

    provider = provider or os.environ.get(PROVIDER_ENV_VAR)
    if provider not in _VALID_PROVIDERS:
        logger.warning(
            "no valid summarizer provider configured (got %r); "
            "returning text unchanged",
            provider,
        )
        return _truncate(text, MAX_CHARS)

    logger.info(
        "summarizing via %s (model=%s, input_len=%d, sentence_count=%d)",
        provider,
        _MODEL_BY_PROVIDER[provider],
        len(text),
        sentence_count,
    )
    summarize_fn = globals()[f"_summarize_{provider}"]
    try:
        # Prompt-building is inside the try too: a custom ENRICHMENT_SUMARIZER_PROMPT
        # may reference a column this call site didn't pass in `fields`
        # (KeyError from str.format) — that should fall back like any other
        # summarization failure, not crash the caller.
        prompt = _prompt(fields, text, sentence_count)
        logger.info("prompt: %s", prompt)
        result = _truncate(summarize_fn(prompt), MAX_CHARS)
        logger.info(
            "summarized via %s: input_len=%d -> output_len=%d",
            provider,
            len(text),
            len(result),
        )
        return result
    except Exception:
        logger.warning(
            "summarization via %s failed, falling back to original text",
            provider,
            exc_info=True,
        )
        return _truncate(text, MAX_CHARS)
