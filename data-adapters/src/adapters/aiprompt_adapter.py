"""AI-prompt adapter: generates content by calling an LLM on a schedule.

Unlike the `api` and `custom` adapters, which relay an external source,
this one *produces* an item from an operator-written prompt — a daily
weather report, a "fact of the day", a rotating safety reminder. It uses
the deployment's configured AI provider exactly as the summarizer does
(`ACTIONS_AI_PROVIDER` / `ACTIONS_AI_*_MODEL` / `ANTHROPIC_API_KEY` /
`OPENAI_API_KEY` — see `adapters.llm.resolve_provider_call`); there are no
per-instance provider/model overrides.

The regeneration schedule ("when to run the prompt again") is the assigned
Policy's fetch stage — the Policy must have `fetch_kind='cron'`, and the
runner wakes this adapter once per occurrence. Everything else about how
the generated item behaves (how it is aired) is the same Policy's transmit
stage.

The item id is keyed to the current cron occurrence AND a fingerprint of
the prompt template: fetch() skips the LLM call entirely when an item with
that id already exists. So an unchanged prompt costs one call per
occurrence, but editing the prompt mints a new id and regenerates on the
very next wake rather than waiting for the next occurrence.

`config` (JSON blob in adapter_instances.config):

    prompt              REQUIRED. instruction sent to the model.
                        safe_format'd with {date} {datetime} {source_name}
                        {source_url}.
    title_template      safe_format'd -> AdapterItem.title
    type / subtype      static passthrough -> items.type / items.subtype
    event_key_template  safe_format'd -> AdapterItem.event_key (default: the
                        occurrence key, so every generation for one slot —
                        across prompt edits — groups together)

Like the API adapter, `config` is parsed fresh on every fetch(), so a UI
edit takes effect on the next wake with no restart.
"""
import hashlib
import logging
from dataclasses import dataclass
from typing import Any

from . import cron
from .base import AdapterItem, DataSourceAdapter, SourceReading
from .llm import resolve_provider_call
from .storage import DEFAULT_DB_PATH, get_connection, get_source_fields, item_exists
from .templating import safe_format
from .timeutil import utc_now

logger = logging.getLogger(__name__)


@dataclass
class AiPromptAdapterConfig:
    prompt: str
    title_template: str = ""
    type: str = ""
    subtype: str = ""
    event_key_template: str = ""

    @classmethod
    def from_dict(cls, config: dict[str, Any]) -> "AiPromptAdapterConfig":
        prompt = (config.get("prompt") or "").strip()
        if not prompt:
            raise ValueError("aiprompt adapter config requires a non-empty 'prompt'")
        return cls(
            prompt=prompt,
            title_template=(config.get("title_template") or "").strip(),
            type=(config.get("type") or "").strip(),
            subtype=(config.get("subtype") or "").strip(),
            event_key_template=(config.get("event_key_template") or "").strip(),
        )


class AiPromptAdapter(DataSourceAdapter):
    """See module docstring. One item per cron occurrence, LLM call skipped
    when that item already exists."""

    def __init__(self, source: str, config: dict[str, Any], *, policy=None, db_path=DEFAULT_DB_PATH):
        self.source = source
        self.config = config
        self.policy = policy
        self.db_path = db_path

    def _cron_expr(self) -> str | None:
        """The regeneration schedule now lives on the assigned Policy's
        fetch stage (fetch_kind='cron'), not in this adapter's config."""
        fetch = getattr(self.policy, "fetch", None)
        if fetch is None or fetch.kind != "cron" or not fetch.cron:
            return None
        return fetch.cron

    def fetch(self) -> SourceReading:
        now = utc_now()
        try:
            cfg = AiPromptAdapterConfig.from_dict(self.config)
        except ValueError as e:
            logger.error("bad aiprompt config source=%s reason=%s", self.source, e)
            return SourceReading(source=self.source, fetched_at=now, ok=False, data=[], error=str(e))

        cron_expr = self._cron_expr()
        if cron_expr is None:
            msg = "aiprompt adapter requires a Policy whose fetch stage is a cron schedule"
            logger.error("%s source=%s", msg, self.source)
            return SourceReading(source=self.source, fetched_at=now, ok=False, data=[], error=msg)

        conn = get_connection(self.db_path)
        try:
            occurrence = cron.latest_fire_at_or_before(cron_expr, now, conn=conn)
            if occurrence is None:
                return SourceReading(
                    source=self.source,
                    fetched_at=now,
                    ok=False,
                    data=[],
                    error=f"could not evaluate cron expression {cron_expr!r}",
                )
            # The id is keyed to BOTH the cron occurrence and a fingerprint of
            # the prompt template, so editing the prompt mints a new id and
            # regenerates on the next poll rather than waiting for the next
            # occurrence — while an unchanged prompt is still one call per
            # occurrence. event_key stays occurrence-only so every generation
            # for the same slot groups together across prompt edits.
            occurrence_key = f"{self.source}-{occurrence.isoformat()}"
            prompt_fingerprint = hashlib.sha1(cfg.prompt.encode("utf-8")).hexdigest()[:8]
            item_id = f"{occurrence_key}-{prompt_fingerprint}"

            if item_exists(conn, self.source, item_id):
                logger.info(
                    "item already generated for this cron occurrence + prompt, skipping LLM call "
                    "source=%s item_id=%s",
                    self.source,
                    item_id,
                )
                return SourceReading(source=self.source, fetched_at=now, ok=True, data=[])

            context = {
                "date": now.date().isoformat(),
                "datetime": now.isoformat(),
                **get_source_fields(conn, self.source),
            }
            prompt = safe_format(cfg.prompt, f"aiprompt[{self.source}].prompt", **context)

            text, provider, model = resolve_provider_call(prompt, conn=conn)
        except Exception as e:  # provider SDKs raise many exception types
            logger.error("aiprompt fetch failed source=%s", self.source, exc_info=True)
            return SourceReading(source=self.source, fetched_at=now, ok=False, data=[], error=str(e))
        finally:
            conn.close()

        text = (text or "").strip()
        if not text:
            return SourceReading(
                source=self.source,
                fetched_at=now,
                ok=False,
                data=[],
                error=f"provider {provider} returned an empty response",
            )

        title = (
            safe_format(cfg.title_template, f"aiprompt[{self.source}].title_template", **context)
            or None
        )
        event_key = (
            safe_format(
                cfg.event_key_template, f"aiprompt[{self.source}].event_key_template", **context
            )
            or occurrence_key
        )
        item = AdapterItem(
            id=item_id,
            title=title,
            contents=text,
            event_key=event_key,
            type=cfg.type or None,
            subtype=cfg.subtype or None,
            source_date_time=now,
            raw={"prompt": prompt, "provider": provider, "model": model},
        )
        logger.info(
            "generated item source=%s item_id=%s provider=%s model=%s",
            self.source, item_id, provider, model,
        )
        return SourceReading(source=self.source, fetched_at=now, ok=True, data=[item])
