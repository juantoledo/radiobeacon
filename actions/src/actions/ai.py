import logging
import sqlite3
from typing import Any

from adapters.storage import get_setting, store_summary

from actions.base import Action

logger = logging.getLogger(__name__)

_DEFAULT_PROMPT = (
    "Resume el siguiente aviso en español, en 2 a 3 oraciones como máximo, "
    "claras y completas — sin inventar información que no esté presente, "
    "y sin dejar ninguna oración a medias.\n\n"
    "Título: {extracted_title}\n"
    "Tipo: {type} / {subtype}\n"
    "Contenido: {extracted_contents}\n"
    "URL: {url}\n\n"
    "Resumen:"
)


def _call_openai(prompt: str, model: str, api_key: str | None) -> str:
    import openai

    client = openai.OpenAI(api_key=api_key) if api_key else openai.OpenAI()
    response = client.chat.completions.create(
        model=model,
        messages=[{"role": "user", "content": prompt}],
    )
    return response.choices[0].message.content


def _call_claude(prompt: str, model: str, api_key: str | None) -> str:
    import anthropic

    client = anthropic.Anthropic(api_key=api_key) if api_key else anthropic.Anthropic()
    response = client.messages.create(
        model=model,
        max_tokens=1024,
        messages=[{"role": "user", "content": prompt}],
    )
    return response.content[0].text


def _call_ollama(prompt: str, model: str, host: str) -> str:
    import ollama

    client = ollama.Client(host=host)
    response = client.chat(model=model, messages=[{"role": "user", "content": prompt}])
    return response["message"]["content"]


class AiAction(Action):
    """On an item.dispatched-shaped event, looks up the item and asks a
    configured LLM provider (OpenAI, Claude, or a self-hosted Ollama) to
    summarize it, storing the result in items.summary (adapters.storage.
    store_summary) and publishing a single item.summarized CloudEvent
    carrying the summary text — unlike chunk's pointer-only completion
    event, this is one bounded value, so there's no reason to force a
    follow-up DB query on every downstream consumer.

    Disabled by default (ACTIONS_AI_ENABLED=false) since, unlike chunk,
    this action has a real per-call cost (a paid API, or a hard
    dependency on a local Ollama install) — opt in explicitly.

    ACTIONS_AI_MAX_CHARS (default 500) is a skip threshold on the INPUT,
    not a cap on the output: if extracted_contents is already at or under
    that length, there's nothing meaningful to condense, so the provider
    is never called at all — cheaper than calling it and getting back
    something close to the original. Whatever the provider actually
    returns is stored and published verbatim (stripped of surrounding
    whitespace only) — never truncated. A model asked to chop its own
    answer to fit a length is far less likely to cut it mid-sentence than
    a hard slice would; see the prompt's own "en 2 a 3 oraciones" framing.

    A provider call failure (network error, bad API key, ...) is
    deliberately NOT caught here and propagates out of run(). __main__.py
    already logs+swallows any exception from run(), but critically does
    NOT record an action.ai.executed audit event when run() raises —
    which is the correct signal for a real failure: "nothing happened,
    see the error log," not a misleadingly successful audit entry with
    no output. Every other skip below (disabled, missing source/item_id,
    item not found, no extracted_contents, contents already short enough,
    bad provider config) is a legitimate no-op, not a failure, so those
    return [] instead."""

    def run(self, event: dict[str, Any], *, conn: sqlite3.Connection) -> list[dict[str, Any]]:
        if get_setting("ACTIONS_AI_ENABLED", "false", conn=conn).lower() != "true":
            return []

        data = event.get("data") or {}
        source, item_id = data.get("source"), data.get("item_id")
        if not source or not item_id:
            logger.warning("ai: event missing source/item_id, skipping: %r", data)
            return []

        row = conn.execute(
            "SELECT extracted_title, extracted_contents, url, type, subtype "
            "FROM items WHERE source = ? AND item_id = ?",
            (source, item_id),
        ).fetchone()
        if row is None:
            logger.info("ai: source=%s item_id=%s not found, skipping", source, item_id)
            return []

        extracted_title, extracted_contents, url, item_type, subtype = row
        if not extracted_contents:
            logger.info(
                "ai: source=%s item_id=%s has no extracted_contents, skipping", source, item_id
            )
            return []

        max_chars = int(get_setting("ACTIONS_AI_MAX_CHARS", "500", conn=conn))
        if len(extracted_contents) <= max_chars:
            logger.info(
                "ai: source=%s item_id=%s extracted_contents already <= %d chars, "
                "nothing to summarize, skipping",
                source,
                item_id,
                max_chars,
            )
            return []

        provider = get_setting("ACTIONS_AI_PROVIDER", conn=conn)
        if provider not in ("openai", "claude", "ollama"):
            logger.error(
                "ai: ACTIONS_AI_PROVIDER=%r invalid (must be openai, claude, or ollama), "
                "skipping",
                provider,
            )
            return []

        prompt_template = get_setting("ACTIONS_AI_PROMPT", _DEFAULT_PROMPT, conn=conn)
        prompt = prompt_template.format(
            extracted_title=extracted_title or "",
            extracted_contents=extracted_contents,
            url=url or "",
            type=item_type or "",
            subtype=subtype or "",
        )

        if provider == "openai":
            model = get_setting("ACTIONS_AI_OPENAI_MODEL", "gpt-4o-mini", conn=conn)
            api_key = get_setting("OPENAI_API_KEY", conn=conn)
            summary = _call_openai(prompt, model, api_key)
        elif provider == "claude":
            model = get_setting("ACTIONS_AI_CLAUDE_MODEL", "claude-haiku-4-5", conn=conn)
            api_key = get_setting("ANTHROPIC_API_KEY", conn=conn)
            summary = _call_claude(prompt, model, api_key)
        else:
            model = get_setting("ACTIONS_AI_OLLAMA_MODEL", "llama3.2:1b", conn=conn)
            host = get_setting("ACTIONS_AI_OLLAMA_HOST", "http://localhost:11434", conn=conn)
            summary = _call_ollama(prompt, model, host)

        summary = summary.strip()

        store_summary(conn, source, item_id, summary)
        logger.info(
            "ai: source=%s item_id=%s summarized via %s: %r", source, item_id, provider, summary
        )

        return [{"source": source, "item_id": item_id, "summary": summary}]
