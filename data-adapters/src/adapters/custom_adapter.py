import html
import logging
import re
import urllib.error
import urllib.request
from datetime import datetime
from typing import Any

from botocore.auth import SigV4Auth
from botocore.awsrequest import AWSRequest
from botocore.credentials import Credentials

from .base import AdapterItem, DataSourceAdapter, SourceReading
from .timeutil import to_utc, utc_now

logger = logging.getLogger(__name__)

_ITEM_FIELDS = (
    "id",
    "title",
    "contents",
    "url",
    "event_key",
    "type",
    "subtype",
    "policy",
    "source_date_time",
    "raw",
)


def _exec_namespace() -> dict[str, Any]:
    """Pre-imported modules made available to a stored CUSTOM code snippet's
    top-level code, so a snippet can be a plain, self-contained script
    (matching how the original hand-written adapters — e.g. senapred —
    were written) without needing its own import boilerplate for the things
    a real-world HTTP/AWS-signed source actually needs."""
    return {
        "__builtins__": __builtins__,
        "html": html,
        "re": re,
        "urllib": urllib,
        "datetime": datetime,
        "SigV4Auth": SigV4Auth,
        "AWSRequest": AWSRequest,
        "Credentials": Credentials,
        "to_utc": to_utc,
        "utc_now": utc_now,
    }


def _build_item(raw_dict: dict[str, Any]) -> AdapterItem:
    if raw_dict.get("id") is None:
        raise ValueError("item missing required 'id'")
    fields = {name: raw_dict.get(name) for name in _ITEM_FIELDS}
    return AdapterItem(**fields)


class CustomAdapter(DataSourceAdapter):
    """Generic "CUSTOM type" adapter: `config["code"]` is a full Python
    source string, trusted-admin-authored (this is a self-hosted,
    single-operator box — no sandboxing), that must define
    `def fetch(config: dict) -> list[dict]`. Each returned dict's keys
    match the adapter item contract (`id` required, the rest optional — see
    adapters.base.AdapterItem). Exists for sources whose fetch logic is
    genuine business logic (auth handshakes, request signing, multi-query
    merging) that can't be reduced to config-only URL/headers/mapping — see
    ApiAdapter for that simpler case.

    `config` (and therefore `config["code"]`) is read fresh on every fetch()
    call, so editing a snippet via the UI takes effect on the very next
    poll, with no process restart required."""

    def __init__(self, source: str, config: dict[str, Any], *, policy=None):
        self.source = source
        self.config = config
        self.policy = policy

    def fetch(self) -> SourceReading:
        now = utc_now()
        try:
            namespace = _exec_namespace()
            exec(self.config["code"], namespace)
            snippet_fetch = namespace.get("fetch")
            if not callable(snippet_fetch):
                raise ValueError("code must define a top-level fetch(config) function")
            raw_items = snippet_fetch(self.config)
            items = []
            for raw_dict in raw_items:
                try:
                    items.append(_build_item(raw_dict))
                except (KeyError, ValueError, TypeError) as e:
                    logger.error(
                        "failed to map item, skipping source=%s reason=%s", self.source, e
                    )
            logger.info("fetch ok source=%s items=%d", self.source, len(items))
            return SourceReading(source=self.source, fetched_at=now, ok=True, data=items)
        except Exception as e:
            # Broad on purpose: arbitrary trusted-admin code can raise
            # anything, and one failed fetch must never crash this
            # instance's poll loop (see adapters.__main__'s per-iteration
            # exception isolation, which this mirrors at the adapter level).
            logger.error("fetch failed source=%s", self.source, exc_info=True)
            return SourceReading(source=self.source, fetched_at=now, ok=False, data=[], error=str(e))
