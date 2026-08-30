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


def _store_summary_or_log(conn: sqlite3.Connection, source: str, item_id: str, summary: str) -> bool:
    """Wraps store_summary with the shared no-op-guard log line -- used
    both when storing a real provider-produced summary and when storing
    an identity copy of extracted_contents on a skip path. A no-op means
    the item vanished (or its key changed) between AiAction's own SELECT
    and this call, e.g. raced by a concurrent adapter re-poll."""
    if store_summary(conn, source, item_id, summary):
        return True
    logger.error(
        "ai: source=%s item_id=%s store_summary found no matching row -- "
        "summary NOT persisted, skipping",
        source,
        item_id,
    )
    return False


class AiAction(Action):
    """On an item.dispatched-shaped event, looks up the item and asks a
    configured LLM provider (OpenAI, Claude, or a self-hosted Ollama) to
    summarize it, ALWAYS storing a result in items.summary (adapters.
    storage.store_summary) and publishing a single CloudEvent to its
    output topic (item.ai_settled by default) carrying a `summarized`
    flag plus, when true, the summary text itself.

    items.summary is populated on every run that finds a real item with
    extracted_contents — even when there's nothing to actually condense
    (AI disabled, content already short, bad provider config): in those
    cases extracted_contents is copied into summary verbatim rather than
    left NULL. This makes summary a reliable single source of truth for
    downstream consumers — most importantly beacon.content.
    resolve_voice_text, which reads items.summary unconditionally with no
    fallback to extracted_contents (see content.py). `summarized` keeps
    its original meaning throughout: True only when a provider actually
    ran and condensed the text; False on every skip path, identity-copy
    or not.

    Unlike most actions, this one ALWAYS publishes once it has a valid
    (source, item_id) — even when it decided there's nothing to
    summarize (item not found, no content, or a store_summary write that
    turned out to be a no-op — see run()): `summarized: False` in all of
    those cases. This is deliberate, not an accident: actions.chunk
    subscribes to this action's output topic (not item.dispatched
    directly) so it only ever chunks AFTER ai has settled — preferring
    the summary when one exists (see chunk.py). If ai stayed silent on
    every skip path (as it used to, returning [] to mean "nothing
    happened"), chunk would never fire at all whenever
    ACTIONS_AI_ENABLED=false — the *default* out-of-the-box state —
    breaking frame content on a fresh clone. Only a genuinely malformed
    event (missing source/item_id entirely) still returns [] — there's
    no (source, item_id) to publish about, and chunk's own independent
    item lookup would find nothing useful either way.

    Disabled by default (ACTIONS_AI_ENABLED=false) since, unlike chunk,
    real summarization has a per-call cost (a paid API, or a hard
    dependency on a local Ollama install) — opt in explicitly. Disabled
    (or any other skip condition) no longer means summary stays empty —
    only that the provider is never called.

    ACTIONS_AI_MAX_CHARS (default 200 — matches ACTIONS_CHUNK_MAX_CHARS's
    default exactly, so both channels share one length budget instead of
    two numbers that happen to both bound length) is a skip threshold on
    the INPUT, not a cap on the output: if extracted_contents is already
    at or under that length, there's nothing meaningful to condense, so
    the provider is never called at all — cheaper than calling it and
    getting back something close to the original; extracted_contents is
    copied into summary as-is instead. Whatever the provider actually
    returns, when it does run, is stored and published verbatim (stripped
    of surrounding whitespace only) — never truncated. A model asked to
    chop its own answer to fit a length is far less likely to cut it
    mid-sentence than a hard slice would; see the prompt's own "en 2 a 3
    oraciones" framing.

    A provider call failure (network error, bad API key, ...) is
    deliberately NOT caught here and propagates out of run(). __main__.py
    already logs+swallows any exception from run(), but critically does
    NOT record an action.ai.executed audit event (nor publish anything)
    when run() raises — which is the correct signal for a real failure:
    "nothing happened, see the error log," not a misleadingly successful
    audit entry. Every skip below is a legitimate no-op, not a failure,
    so those still publish (with summarized: False) rather than raising."""

    default_subscribe_topic = "radiobeacon/events/item.dispatched"
    default_output_topic = "radiobeacon/events/item.ai_settled"
    default_output_event_type = "item.ai_settled"

    def run(self, event: dict[str, Any], *, conn: sqlite3.Connection) -> list[dict[str, Any]]:
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
            return [{"source": source, "item_id": item_id, "summarized": False}]

        extracted_title, extracted_contents, url, item_type, subtype = row
        if not extracted_contents:
            logger.info(
                "ai: source=%s item_id=%s has no extracted_contents, skipping", source, item_id
            )
            return [{"source": source, "item_id": item_id, "summarized": False}]

        max_chars = int(get_setting("ACTIONS_AI_MAX_CHARS", "200", conn=conn))
        provider = get_setting("ACTIONS_AI_PROVIDER", conn=conn)

        skip_reason = None
        if get_setting("ACTIONS_AI_ENABLED", "false", conn=conn).lower() != "true":
            skip_reason = "ACTIONS_AI_ENABLED is not true"
        elif len(extracted_contents) <= max_chars:
            skip_reason = f"extracted_contents already <= {max_chars} chars, nothing to summarize"
        elif provider not in ("openai", "claude", "ollama"):
            skip_reason = (
                f"ACTIONS_AI_PROVIDER={provider!r} invalid (must be openai, claude, or ollama)"
            )

        if skip_reason is not None:
            logger.info(
                "ai: source=%s item_id=%s %s, storing extracted_contents as summary verbatim",
                source,
                item_id,
                skip_reason,
            )
            _store_summary_or_log(conn, source, item_id, extracted_contents)
            return [{"source": source, "item_id": item_id, "summarized": False}]

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

        if not _store_summary_or_log(conn, source, item_id, summary):
            # store_summary's UPDATE matched no row -- the item vanished
            # (or its (source, item_id) key changed) between the SELECT
            # above and here, e.g. raced by a concurrent adapter re-poll
            # upserting the same item. Publishing summarized: True here
            # regardless (as this used to) would tell chunk a summary
            # exists when items.summary is actually still whatever it was
            # before (often NULL) -- chunk would then silently fall back
            # to extracted_contents while ai's own log/audit trail claims
            # success. Treating it as an ordinary skip (summarized: False)
            # keeps that contract honest and matches every other skip path
            # above, which already publish rather than raise.
            return [{"source": source, "item_id": item_id, "summarized": False}]

        logger.info(
            "ai: source=%s item_id=%s summarized via %s: %r", source, item_id, provider, summary
        )

        return [{"source": source, "item_id": item_id, "summarized": True, "summary": summary}]
