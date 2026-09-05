import json
import logging
import sqlite3
from typing import Any

from adapters.actions_defaults import AI_PROMPT_DEFAULT
from adapters.llm import call_claude as _call_claude
from adapters.llm import call_ollama as _call_ollama
from adapters.llm import call_openai as _call_openai
from adapters.storage import (
    get_adapter_instance,
    get_setting,
    get_source_fields,
    store_summary,
)

from actions.base import Action

logger = logging.getLogger(__name__)

# The provider clients live in adapters.llm now, shared with the aiprompt
# adapter. Imported under their historical private names so this module's
# call sites (and the tests that monkeypatch them) are unchanged.


# Every item column the AI prompt template can reference as a
# {placeholder}. The summarizer selects exactly these from `items` and
# passes them all to str.format, so an operator's per-adapter "AI prompt
# override" (or the global ACTIONS_AI_PROMPT) can build a prompt out of
# any of an item's mapped adapter attributes, not just its title/contents.
# `rawdata` is the original unmapped source payload (a JSON string).
#
# Two more placeholders are merged in on top of these from the `sources`
# table (adapters.storage.get_source_fields), so the same {source_name} /
# {source_url} an operator already uses in the beacon/chunk templates work
# in the AI prompt too: {source_name} is the source's human display name
# (falls back to the raw source key when unmanaged), {source_url} its site
# URL.
PROMPT_ITEM_FIELDS = (
    "source",
    "item_id",
    "extracted_title",
    "extracted_contents",
    "summary",
    "url",
    "event_key",
    "type",
    "subtype",
    "transmit_policy",
    "source_date_time",
    "fetched_at",
    "captured_at",
    "rawdata",
)


class _BlankForMissing(dict):
    """format_map backing dict that renders an unknown {placeholder} as ""
    instead of raising KeyError — the prompt template is operator-editable
    (per-adapter on the /adapters form, or globally via ACTIONS_AI_PROMPT),
    so a stray placeholder shouldn't crash the summarizer."""

    def __missing__(self, key: str) -> str:
        return ""


def _render_prompt(template: str, fields: dict[str, Any], *, source: str) -> str:
    """Fills `template` from `fields` (see PROMPT_ITEM_FIELDS). Unknown
    {placeholder}s render blank; a genuinely malformed template (an
    unescaped literal brace) logs and falls back to the built-in default
    prompt rather than propagating out of run() as a fetch-style failure."""
    try:
        return template.format_map(_BlankForMissing(fields))
    except (ValueError, IndexError) as exc:
        logger.error(
            "ai: source=%s prompt template %r is invalid (%s); "
            "falling back to the built-in default prompt",
            source,
            template,
            exc,
        )
        return AI_PROMPT_DEFAULT.format_map(_BlankForMissing(fields))


def _resolve_prompt_template(conn: sqlite3.Connection, source: str) -> str:
    """Prompt-template resolution order: the source's own adapter_instances
    config `ai_prompt` (an optional per-adapter override, set on the
    /adapters form) -> the global ACTIONS_AI_PROMPT setting -> the built-in
    AI_PROMPT_DEFAULT. A source with no adapter_instances row (e.g. an item
    from a since-deleted source) falls straight through to the global path.
    """
    instance = get_adapter_instance(conn, source)
    if instance:
        config = json.loads(instance["config"]) or {}
        adapter_prompt = config.get("ai_prompt")
        if adapter_prompt:
            return adapter_prompt
    return get_setting("ACTIONS_AI_PROMPT", AI_PROMPT_DEFAULT, conn=conn)


def _resolve_fallback_to_title(conn: sqlite3.Connection, source: str) -> bool:
    """Per-adapter opt-in (adapter_instances.config `ai_fallback_to_title`, set
    on the /adapters form): when true, the item's title is stored as the summary
    instead of the full extracted_contents whenever the AI summarizer can't run
    — a provider call that raises is caught so the pipeline keeps flowing, and
    the AI-disabled skip stores the title rather than copying extracted_contents
    verbatim. When false (the default, and any source with no adapter_instances
    row) a provider failure propagates as before and the item stops, and the
    disabled skip copies extracted_contents. There is deliberately no global
    setting — it's an adapter-by-adapter call."""
    instance = get_adapter_instance(conn, source)
    if not instance:
        return False
    config = json.loads(instance["config"]) or {}
    return bool(config.get("ai_fallback_to_title", False))


def _settled(
    source: str,
    item_id: str,
    *,
    summarized: bool,
    reason: str | None = None,
    **extra: Any,
) -> list[dict[str, Any]]:
    """Builds AiAction's single output payload (which becomes the
    item.ai_settled CloudEvent's `data`). `source`/`item_id`/`summarized`
    are the load-bearing fields every consumer relies on; everything else
    is diagnostic detail carried for humans reading the event stream or
    the audit log:

      - `reason`: on every `summarized: False` path, a short phrase saying
        why nothing was summarized (item missing, AI disabled, content
        already short, ...).
      - `provider` / `model`: which backend actually produced the summary
        (only on `summarized: True`).
      - `prompt`: the fully rendered prompt sent to the provider — only
        included when ACTIONS_AI_EVENT_INCLUDE_PROMPT is true, since it
        embeds the item's full extracted_contents and would otherwise
        bloat every event/log line.

    __main__.py copies each of these (see _AUDIT_DETAIL_KEYS) from the
    output into the action.ai.executed audit row, so they show up on
    /audit — `prompt` there too, on the same ACTIONS_AI_EVENT_INCLUDE_PROMPT
    opt-in.
    """
    payload: dict[str, Any] = {"source": source, "item_id": item_id, "summarized": summarized}
    if reason is not None:
        payload["reason"] = reason
    payload.update(extra)
    return [payload]


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
    left NULL (a source with config.ai_fallback_to_title stores the item's
    title instead on the AI-disabled, no-valid-provider, and provider-failure
    paths — see _resolve_fallback_to_title). This makes summary a reliable single source of truth for
    downstream consumers — most importantly beacon.content.
    resolve_voice_text, which reads items.summary unconditionally with no
    fallback to extracted_contents (see content.py). `summarized` keeps
    its original meaning throughout: True only when a provider actually
    ran and condensed the text; False on every skip path, identity-copy
    or not.

    Beyond that contract, every emitted event carries diagnostic detail
    (see _settled): a `reason` phrase on every `summarized: False` path
    (item missing, AI disabled, content already short, ...), `provider`/
    `model` on success, and — only when ACTIONS_AI_EVENT_INCLUDE_PROMPT
    is true — the fully rendered `prompt`. __main__.py mirrors all of
    these into the action.ai.executed audit row so they're visible on
    /audit without cross-referencing logs.

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

    The prompt template is resolved per item (see _resolve_prompt_template):
    the source's own adapter_instances config `ai_prompt` wins when set — a
    per-adapter override so e.g. a SENAPRED alert and a CSN bulletin can be
    framed differently — otherwise the global ACTIONS_AI_PROMPT setting,
    otherwise the built-in AI_PROMPT_DEFAULT. Whichever template wins, it can
    reference any of the item's mapped adapter attributes as a
    {placeholder} (see PROMPT_ITEM_FIELDS / _render_prompt) — not just
    title/contents — plus the source's display name / site URL as
    {source_name} / {source_url} (the same placeholders the beacon & chunk
    templates use). An unknown placeholder renders blank rather than
    crashing the summarizer.

    A provider call failure (network error, bad API key, ...) is by default
    deliberately NOT caught here and propagates out of run(). __main__.py
    already logs+swallows any exception from run(), but critically does
    NOT record an action.ai.executed audit event (nor publish anything)
    when run() raises — which is the correct signal for a real failure:
    "nothing happened, see the error log," not a misleadingly successful
    audit entry. Every skip below is a legitimate no-op, not a failure,
    so those still publish (with summarized: False) rather than raising.

    A source can opt out of that hard stop with the per-adapter
    adapter_instances.config `ai_fallback_to_title` flag (see
    _resolve_fallback_to_title): when it is true, a provider failure is
    caught, the item's title (falling back to extracted_contents) is stored
    as items.summary, and an ordinary summarized: False skip is published so
    chunk still runs and the item stays on air — the right trade-off for a
    time-critical source where something beats nothing."""

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
            f"SELECT {', '.join(PROMPT_ITEM_FIELDS)} "
            "FROM items WHERE source = ? AND item_id = ?",
            (source, item_id),
        ).fetchone()
        if row is None:
            logger.info("ai: source=%s item_id=%s not found, skipping", source, item_id)
            return _settled(
                source, item_id, summarized=False, reason="item not found in items table"
            )

        # Every mapped adapter attribute is available to the prompt
        # template (see PROMPT_ITEM_FIELDS); NULLs render as "". The
        # source's display name / site URL are merged in as {source_name} /
        # {source_url}, the same placeholders the beacon & chunk templates use.
        item_fields = {
            name: ("" if value is None else value) for name, value in zip(PROMPT_ITEM_FIELDS, row)
        }
        item_fields.update(get_source_fields(conn, source))
        extracted_contents = item_fields["extracted_contents"]
        if not extracted_contents:
            logger.info(
                "ai: source=%s item_id=%s has no extracted_contents, skipping", source, item_id
            )
            return _settled(
                source, item_id, summarized=False, reason="item has no extracted_contents"
            )

        max_chars = int(get_setting("ACTIONS_AI_MAX_CHARS", "200", conn=conn))
        provider = get_setting("ACTIONS_AI_PROVIDER", conn=conn)

        skip_reason = None
        ai_disabled = get_setting("ACTIONS_AI_ENABLED", "false", conn=conn).lower() != "true"
        no_provider = provider not in ("openai", "claude", "ollama")
        if ai_disabled:
            skip_reason = "ACTIONS_AI_ENABLED is not true"
        elif len(extracted_contents) <= max_chars:
            skip_reason = f"extracted_contents already <= {max_chars} chars, nothing to summarize"
        elif no_provider:
            skip_reason = (
                f"ACTIONS_AI_PROVIDER={provider!r} invalid (must be openai, claude, or ollama)"
            )

        if skip_reason is not None:
            # A source that opts in via config.ai_fallback_to_title stores the
            # item's title (not the full extracted_contents) as the summary
            # whenever AI isn't actually going to run at all -- AI disabled, or
            # enabled with no usable provider configured -- the same
            # substitution it makes on a provider call failure, see
            # _resolve_fallback_to_title. The content-already-short skip keeps
            # copying extracted_contents verbatim, since there the content
            # itself is already a fine summary.
            fallback_body = extracted_contents
            if (ai_disabled or no_provider) and _resolve_fallback_to_title(conn, source):
                fallback_body = item_fields["extracted_title"] or extracted_contents
                logger.info(
                    "ai: source=%s item_id=%s %s and ai_fallback_to_title set, "
                    "storing title as summary",
                    source,
                    item_id,
                    "AI disabled" if ai_disabled else "no valid AI provider configured",
                )
            else:
                logger.info(
                    "ai: source=%s item_id=%s %s, storing extracted_contents as summary verbatim",
                    source,
                    item_id,
                    skip_reason,
                )
            _store_summary_or_log(conn, source, item_id, fallback_body)
            return _settled(source, item_id, summarized=False, reason=skip_reason)

        prompt_template = _resolve_prompt_template(conn, source)
        prompt = _render_prompt(prompt_template, item_fields, source=source)

        if provider == "openai":
            model = get_setting("ACTIONS_AI_OPENAI_MODEL", "gpt-4o-mini", conn=conn)
        elif provider == "claude":
            model = get_setting("ACTIONS_AI_CLAUDE_MODEL", "claude-haiku-4-5", conn=conn)
        else:
            model = get_setting("ACTIONS_AI_OLLAMA_MODEL", "llama3.2:1b", conn=conn)

        try:
            if provider == "openai":
                summary = _call_openai(prompt, model, get_setting("OPENAI_API_KEY", conn=conn))
            elif provider == "claude":
                summary = _call_claude(prompt, model, get_setting("ANTHROPIC_API_KEY", conn=conn))
            else:
                host = get_setting("ACTIONS_AI_OLLAMA_HOST", "http://localhost:11434", conn=conn)
                summary = _call_ollama(prompt, model, host)
        except Exception as exc:
            # A provider failure (network, bad key, rate limit, ...) stops the
            # item by default -- the exception propagates, __main__.py records
            # no audit row and publishes nothing, so chunk never fires. A
            # source that opts in via config.ai_fallback_to_title instead keeps
            # flowing: the item's title is stored as the summary and a normal
            # summarized:False skip is published (see _resolve_fallback_to_title).
            if not _resolve_fallback_to_title(conn, source):
                raise
            fallback = item_fields["extracted_title"] or extracted_contents
            logger.warning(
                "ai: source=%s item_id=%s provider %s call failed (%s); "
                "storing title as summary and continuing",
                source,
                item_id,
                provider,
                exc,
            )
            _store_summary_or_log(conn, source, item_id, fallback)
            return _settled(
                source,
                item_id,
                summarized=False,
                reason=f"provider call failed ({exc}); stored title as summary",
                provider=provider,
                model=model,
            )

        summary = summary.strip()

        # Diagnostic detail attached to the emitted event (see _settled).
        # provider/model are always cheap to carry; the rendered prompt is
        # opt-in since it duplicates the item's full extracted_contents.
        detail: dict[str, Any] = {"provider": provider, "model": model}
        if get_setting("ACTIONS_AI_EVENT_INCLUDE_PROMPT", "false", conn=conn).lower() == "true":
            detail["prompt"] = prompt

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
            return _settled(
                source,
                item_id,
                summarized=False,
                reason="summary produced but store_summary matched no row "
                "(item changed concurrently after summarization)",
                **detail,
            )

        logger.info(
            "ai: source=%s item_id=%s summarized via %s: %r", source, item_id, provider, summary
        )

        return _settled(source, item_id, summarized=True, summary=summary, **detail)
