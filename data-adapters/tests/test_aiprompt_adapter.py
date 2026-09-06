import hashlib
from datetime import datetime, timezone

import adapters.aiprompt_adapter as aiprompt_module
from adapters.aiprompt_adapter import AiPromptAdapter
from adapters.base import AdapterItem, SourceReading
from adapters.policy import Policy, Schedule
from adapters.storage import get_connection, store_reading

EVERY_MINUTE = "* * * * *"
DEFAULT_PROMPT = "Give a fact for {date}."

CRON_POLICY = Policy(
    name="aip",
    fetch=Schedule(kind="cron", cron=EVERY_MINUTE),
    transmit=Schedule(kind="once", count=1),
)
NON_CRON_POLICY = Policy(
    name="default",
    fetch=Schedule(kind="interval", interval_seconds=10),
    transmit=Schedule(kind="once", count=1),
)


def _cfg(**over):
    base = {"prompt": DEFAULT_PROMPT}
    base.update(over)
    return base


def _adapter(source, cfg, db_path, policy=CRON_POLICY):
    return AiPromptAdapter(source, cfg, policy=policy, db_path=db_path)


def _fingerprint(prompt):
    return hashlib.sha1(prompt.encode("utf-8")).hexdigest()[:8]


def _pin_occurrence(monkeypatch, occurrence):
    monkeypatch.setattr(
        aiprompt_module.cron, "latest_fire_at_or_before",
        lambda expr, at, *, conn: occurrence,
    )


def _stub_call(text="A generated fact.", provider="claude", model="claude-haiku-4-5"):
    calls = []

    def _call(prompt, *, conn):
        calls.append({"prompt": prompt})
        return text, provider, model

    _call.calls = calls
    return _call


def test_happy_path_produces_one_item(tmp_path, monkeypatch):
    db_path = tmp_path / "radiobeacon.db"
    get_connection(db_path)
    occurrence = datetime(2026, 9, 3, 6, 0, tzinfo=timezone.utc)
    _pin_occurrence(monkeypatch, occurrence)
    stub = _stub_call(text="  A generated fact.  ")
    monkeypatch.setattr(aiprompt_module, "resolve_provider_call", stub)

    reading = _adapter("wx", _cfg(), db_path).fetch()

    assert reading.ok is True
    item = reading.data[0]
    assert item.id == f"wx-{occurrence.isoformat()}-{_fingerprint(DEFAULT_PROMPT)}"
    assert item.contents == "A generated fact."
    assert item.event_key == f"wx-{occurrence.isoformat()}"
    assert len(stub.calls) == 1


def test_skips_llm_call_when_same_occurrence_and_prompt_item_exists(tmp_path, monkeypatch):
    db_path = tmp_path / "radiobeacon.db"
    conn = get_connection(db_path)
    occurrence = datetime(2026, 9, 3, 6, 0, tzinfo=timezone.utc)
    _pin_occurrence(monkeypatch, occurrence)
    existing_id = f"wx-{occurrence.isoformat()}-{_fingerprint(DEFAULT_PROMPT)}"
    store_reading(
        conn,
        SourceReading(
            source="wx",
            fetched_at=datetime.now(timezone.utc),
            ok=True,
            data=[AdapterItem(id=existing_id, contents="already generated")],
        ),
    )
    stub = _stub_call()
    monkeypatch.setattr(aiprompt_module, "resolve_provider_call", stub)

    reading = _adapter("wx", _cfg(), db_path).fetch()

    assert reading.ok is True
    assert reading.data == []
    assert stub.calls == []


def test_editing_the_prompt_regenerates_within_the_same_occurrence(tmp_path, monkeypatch):
    db_path = tmp_path / "radiobeacon.db"
    conn = get_connection(db_path)
    occurrence = datetime(2026, 9, 3, 6, 0, tzinfo=timezone.utc)
    _pin_occurrence(monkeypatch, occurrence)
    store_reading(
        conn,
        SourceReading(
            source="wx",
            fetched_at=datetime.now(timezone.utc),
            ok=True,
            data=[AdapterItem(
                id=f"wx-{occurrence.isoformat()}-{_fingerprint('old prompt')}",
                contents="old",
            )],
        ),
    )
    stub = _stub_call(text="new output")
    monkeypatch.setattr(aiprompt_module, "resolve_provider_call", stub)

    reading = _adapter("wx", _cfg(prompt="a brand new prompt"), db_path).fetch()

    assert len(stub.calls) == 1
    item = reading.data[0]
    assert item.id == f"wx-{occurrence.isoformat()}-{_fingerprint('a brand new prompt')}"
    assert item.contents == "new output"


def test_new_occurrence_generates_a_fresh_item(tmp_path, monkeypatch):
    db_path = tmp_path / "radiobeacon.db"
    conn = get_connection(db_path)
    store_reading(
        conn,
        SourceReading(
            source="wx",
            fetched_at=datetime.now(timezone.utc),
            ok=True,
            data=[AdapterItem(
                id=f"wx-2026-09-03T06:00:00+00:00-{_fingerprint(DEFAULT_PROMPT)}",
                contents="yesterday",
            )],
        ),
    )
    later = datetime(2026, 9, 4, 6, 0, tzinfo=timezone.utc)
    _pin_occurrence(monkeypatch, later)
    stub = _stub_call(text="today's report")
    monkeypatch.setattr(aiprompt_module, "resolve_provider_call", stub)

    reading = _adapter("wx", _cfg(), db_path).fetch()

    assert [i.id for i in reading.data] == [
        f"wx-{later.isoformat()}-{_fingerprint(DEFAULT_PROMPT)}"
    ]
    assert len(stub.calls) == 1


def test_non_cron_policy_is_rejected(tmp_path):
    db_path = tmp_path / "radiobeacon.db"
    get_connection(db_path)
    reading = _adapter("wx", _cfg(), db_path, policy=NON_CRON_POLICY).fetch()
    assert reading.ok is False
    assert "cron" in reading.error.lower()


def test_missing_policy_is_rejected(tmp_path):
    db_path = tmp_path / "radiobeacon.db"
    get_connection(db_path)
    reading = _adapter("wx", _cfg(), db_path, policy=None).fetch()
    assert reading.ok is False
    assert "cron" in reading.error.lower()


def test_blank_prompt_is_rejected(tmp_path):
    db_path = tmp_path / "radiobeacon.db"
    get_connection(db_path)
    reading = _adapter("wx", {"prompt": "  "}, db_path).fetch()
    assert reading.ok is False
    assert "prompt" in reading.error


def test_provider_failure_is_reported_not_raised(tmp_path, monkeypatch):
    db_path = tmp_path / "radiobeacon.db"
    get_connection(db_path)

    def _boom(*args, **kwargs):
        raise RuntimeError("rate limited")

    monkeypatch.setattr(aiprompt_module, "resolve_provider_call", _boom)

    reading = _adapter("wx", _cfg(), db_path).fetch()
    assert reading.ok is False
    assert "rate limited" in reading.error
    assert reading.data == []


def test_empty_response_is_a_failure(tmp_path, monkeypatch):
    db_path = tmp_path / "radiobeacon.db"
    get_connection(db_path)
    monkeypatch.setattr(aiprompt_module, "resolve_provider_call", _stub_call(text="   "))

    reading = _adapter("wx", _cfg(), db_path).fetch()
    assert reading.ok is False
    assert "empty response" in reading.error


def test_item_shaping_fields_flow_through(tmp_path, monkeypatch):
    db_path = tmp_path / "radiobeacon.db"
    get_connection(db_path)
    stub = _stub_call()
    monkeypatch.setattr(aiprompt_module, "resolve_provider_call", stub)

    adapter = _adapter(
        "greet",
        _cfg(
            prompt="Greeting for {source_name}.",
            title_template="Greeting — {date}",
            type="greeting",
            event_key_template="greet-static",
        ),
        db_path,
    )
    reading = adapter.fetch()

    item = reading.data[0]
    assert item.title.startswith("Greeting — ")
    assert item.type == "greeting"
    assert item.event_key == "greet-static"
    assert stub.calls[0]["prompt"].startswith("Greeting for ")
