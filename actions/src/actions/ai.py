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
    record_audit_event,
    store_summary,
)

# adapter_instances.config.ai_on_failure — what actions.ai does when it can't
# produce a real provider summary. See _resolve_ai_on_failure.
AI_ON_FAILURE_MODES = ("continue_with_contents", "use_title", "abort")
_DEFAULT_AI_ON_FAILURE = "continue_with_contents"

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
    "policy",
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
            "prompt template invalid, falling back to built-in default source=%s template=%r",
            source,
            template,
            exc_info=True,
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


def _resolve_ai_on_failure(conn: sqlite3.Connection, source: str) -> str:
    """Per-adapter choice (adapter_instances.config `ai_on_failure`, set on the
    /adapters form's "On AI failure" dropdown) for what to do when there is no
    real provider summary:

      - "continue_with_contents" (default, and any source with no
        adapter_instances row): store `extracted_contents` verbatim as the
        summary and keep the pipeline flowing. A provider call that *raises*
        still propagates and stalls the item (unchanged) — pick "abort" for an
        observable stop.
      - "use_title": store the item's `title` (falling back to
        `extracted_contents`) and keep flowing; a provider exception is caught.
      - "abort": store nothing, emit an `item.ai_aborted` audit event, and stop
        — the item is never chunked or transmitted. For sources whose raw
        `extracted_contents` is meaningless without AI.

    Honours the legacy boolean `ai_fallback_to_title` (true -> "use_title") so
    a config written before this dropdown existed keeps working."""
    instance = get_adapter_instance(conn, source)
    if not instance:
        return _DEFAULT_AI_ON_FAILURE
    config = json.loads(instance["config"]) or {}
    mode = config.get("ai_on_failure")
    if mode in AI_ON_FAILURE_MODES:
        return mode
    if config.get("ai_fallback_to_title"):
        return "use_title"
    return _DEFAULT_AI_ON_FAILURE


def _resolve_use_ai(conn: sqlite3.Connection, source: str) -> bool:
    """Per-adapter AI on/off (adapter_instances.config `use_ai`, set on the
    /adapters form's "Use AI" checkbox) — lets an adapter whose mapping
    already extracts short, well-formed contents (e.g. a dotted-path
    template pulling exactly the field wanted — see
    adapters.api_adapter.resolve_dotted_path) skip AI summarization
    entirely, independent of the global ACTIONS_AI_ENABLED switch. Missing
    key, or no adapter_instances row at all, -> True, so every adapter that
    predates this field keeps being summarized exactly as before."""
    instance = get_adapter_instance(conn, source)
    if not instance:
        return True
    config = json.loads(instance["config"]) or {}
    return bool(config.get("use_ai", True))


def _handle_no_summary(
    conn: sqlite3.Connection,
    source: str,
    item_id: str,
    *,
    mode: str,
    reason: str,
    extracted_contents: str,
    title: str,
    title_ok: bool,
    **extra: Any,
) -> list[dict[str, Any]]:
    """Shared tail for every path where actions.ai has no real provider
    summary. `abort` records `item.ai_aborted` and returns [] (nothing is
    published -> chunk never runs -> content_ready never fires -> the item
    never airs). Otherwise a fallback body is stored and an ordinary
    `summarized: False` settle is published so the pipeline continues:
    `use_title` (when `title_ok` — i.e. this is a genuine can't-run/failed
    path, not the content-already-short skip) uses the title, everything else
    copies `extracted_contents`."""
    if mode == "abort":
        record_audit_event(
            conn,
            event_type="item.ai_aborted",
            actor="actions.ai",
            source=source,
            item_id=item_id,
            details={"reason": reason},
        )
        logger.info(
            "aborted, not chunked or transmitted source=%s item_id=%s reason=%s",
            source, item_id, reason,
        )
        return []

    body = title or extracted_contents if (mode == "use_title" and title_ok) else extracted_contents
    _store_summary_or_log(conn, source, item_id, body)
    return _settled(source, item_id, summarized=False, reason=reason, **extra)


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
        "store_summary found no matching row, summary not persisted source=%s item_id=%s",
        source,
        item_id,
    )
    return False


class AiAction(Action):
    """On an item.dispatched-shaped event, looks up the item and asks a
    configured LLM provider (OpenAI, Claude, or a self-hosted Ollama) to
    summarize it, storing the result in items.summary (adapters.storage.
    store_summary) and publishing a single CloudEvent to its output topic
    (item.ai_settled by default) carrying a `summarized` flag plus, when
    true, the summary text itself.

    items.summary is normally populated on every run that finds a real item
    with extracted_contents — even when there's nothing to condense (AI
    disabled, content already short, bad provider config): extracted_contents
    is copied verbatim, or the item's title when config.ai_on_failure is
    "use_title" (see _resolve_ai_on_failure). The exception is
    config.ai_on_failure="abort": summary stays NULL, item.ai_settled is not
    published, and the item never airs. summary is otherwise a reliable
    single source of truth for downstream consumers — most importantly
    beacon.content.
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

    A per-adapter "Use AI" checkbox (adapter_instances.config `use_ai`,
    see _resolve_use_ai) sits alongside the global switch: an adapter whose
    mapping already produces short, well-formed contents on its own (e.g. a
    dotted-path template pulling exactly the field wanted) can opt out of
    summarization entirely, even while ACTIONS_AI_ENABLED stays on for
    every other adapter. Missing the key (any adapter that predates it)
    means True — summarized exactly as before. Unlike every other skip
    reason, this one always stores extracted_contents verbatim and never
    consults ai_on_failure: unchecking Use AI is a deliberate "this
    adapter's contents are fine without AI" declaration, not a failure, so
    an adapter left configured with ai_on_failure=abort (set back when it
    still relied on AI) doesn't have every item silently dropped the
    moment its own toggle goes off.

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

    What happens when there is no real provider summary is a per-adapter
    choice — adapter_instances.config `ai_on_failure` (see
    _resolve_ai_on_failure), set on the /adapters form:

      - "continue_with_contents" (default): the benign skips (AI disabled,
        content already short, misconfigured provider) and an empty provider
        response store `extracted_contents` verbatim and publish an ordinary
        summarized: False settle so chunk still runs. A provider call that
        *raises* is deliberately NOT caught — it propagates, __main__ records
        no action.ai.executed row and publishes nothing, and the item stalls
        until a rearm. Pick "abort" for an observable stop instead.
      - "use_title": stores the item's title (falling back to
        extracted_contents) on every can't-run / failed path, catching a
        provider exception too, so a time-critical source stays on air.
      - "abort": stores nothing, records an item.ai_aborted audit event, and
        returns [] — no item.ai_settled is published, so chunk never runs and
        the item is never transmitted. For sources whose raw extracted_contents
        is meaningless without AI."""

    default_subscribe_topic = "radiobeacon/events/item.dispatched"
    default_output_topic = "radiobeacon/events/item.ai_settled"
    default_output_event_type = "item.ai_settled"

    def run(self, event: dict[str, Any], *, conn: sqlite3.Connection) -> list[dict[str, Any]]:
        data = event.get("data") or {}
        source, item_id = data.get("source"), data.get("item_id")
        if not source or not item_id:
            logger.warning("event missing source/item_id, skipping data=%r", data)
            return []

        row = conn.execute(
            f"SELECT {', '.join(PROMPT_ITEM_FIELDS)} "
            "FROM items WHERE source = ? AND item_id = ?",
            (source, item_id),
        ).fetchone()
        if row is None:
            logger.info("item not found, skipping source=%s item_id=%s", source, item_id)
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
                "item has no extracted_contents, skipping source=%s item_id=%s", source, item_id
            )
            return _settled(
                source, item_id, summarized=False, reason="item has no extracted_contents"
            )

        max_chars = int(get_setting("ACTIONS_AI_MAX_CHARS", "200", conn=conn))
        provider = get_setting("ACTIONS_AI_PROVIDER", conn=conn)

        skip_reason = None
        ai_disabled = get_setting("ACTIONS_AI_ENABLED", "false", conn=conn).lower() != "true"
        adapter_ai_disabled = not _resolve_use_ai(conn, source)
        no_provider = provider not in ("openai", "claude", "ollama")
        if ai_disabled:
            skip_reason = "ACTIONS_AI_ENABLED is not true"
        elif adapter_ai_disabled:
            skip_reason = "AI disabled for this adapter (Use AI unchecked)"
        elif len(extracted_contents) <= max_chars:
            skip_reason = f"extracted_contents already <= {max_chars} chars, nothing to summarize"
        elif no_provider:
            skip_reason = (
                f"ACTIONS_AI_PROVIDER={provider!r} invalid (must be openai, claude, or ollama)"
            )

        if skip_reason is not None:
            # `use_title` only substitutes the title where AI genuinely isn't
            # going to run (disabled / no usable provider); the
            # content-already-short skip keeps copying extracted_contents
            # verbatim, since there the content itself is a fine summary.
            #
            # Use AI unchecked is different from every other skip reason
            # here: it's a deliberate per-adapter declaration that the
            # mapped contents are already good without AI, not a failure —
            # so it must never honor ai_on_failure's "abort"/"use_title"
            # choices (those exist for a genuine AI *failure*, where the
            # operator decided raw contents might be meaningless on their
            # own). Forcing continue_with_contents here is what stops an
            # adapter configured with ai_on_failure=abort from having every
            # item silently dropped the moment its own AI toggle is
            # unchecked.
            mode = "continue_with_contents" if adapter_ai_disabled else _resolve_ai_on_failure(conn, source)
            return _handle_no_summary(
                conn, source, item_id,
                mode=mode,
                reason=skip_reason,
                extracted_contents=extracted_contents,
                title=item_fields["extracted_title"],
                title_ok=ai_disabled or no_provider,
            )

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
            # `continue_with_contents` (default) lets a provider exception
            # propagate -- __main__ records no audit row and publishes nothing,
            # so chunk never fires and the item stalls until a rearm. `use_title`
            # catches it and keeps flowing; `abort` catches it and stops the
            # item observably (item.ai_aborted).
            mode = _resolve_ai_on_failure(conn, source)
            if mode == "continue_with_contents":
                raise
            logger.warning(
                "provider call failed source=%s item_id=%s provider=%s ai_on_failure=%s",
                source, item_id, provider, mode, exc_info=True,
            )
            return _handle_no_summary(
                conn, source, item_id,
                mode=mode,
                reason=f"provider call failed ({exc})",
                extracted_contents=extracted_contents,
                title=item_fields["extracted_title"],
                title_ok=True,
                provider=provider,
                model=model,
            )

        summary = summary.strip()
        if not summary:
            # An empty provider response is not a real summary — route it
            # through the same per-adapter choice as a skip / failure rather
            # than airing a blank bulletin (which is what publishing
            # summarized: True with summary="" used to do).
            return _handle_no_summary(
                conn, source, item_id,
                mode=_resolve_ai_on_failure(conn, source),
                reason=f"provider {provider} returned an empty response",
                extracted_contents=extracted_contents,
                title=item_fields["extracted_title"],
                title_ok=True,
                provider=provider,
                model=model,
            )

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
            "summarized source=%s item_id=%s provider=%s summary=%r", source, item_id, provider, summary
        )

        return _settled(source, item_id, summarized=True, summary=summary, **detail)
