import pytest

from adapters.storage import get_connection, set_adapter_instance, set_setting

import actions.ai
from actions.ai import AiAction


def _insert_item(
    conn,
    source,
    item_id,
    extracted_contents,
    *,
    extracted_title=None,
    url=None,
    item_type=None,
    subtype=None,
):
    conn.execute(
        "INSERT INTO items (source, item_id, extracted_title, extracted_contents, url, "
        "type, subtype, fetched_at, rawdata) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            source,
            item_id,
            extracted_title,
            extracted_contents,
            url,
            item_type,
            subtype,
            "2026-08-19T12:00:00",
            "{}",
        ),
    )
    conn.commit()


def _dispatched_event(source, item_id):
    return {
        "specversion": "1.0",
        "type": "cl.radiobeacon.item.dispatched",
        "source": "radiobeacon/log_handler",
        "data": {"source": source, "item_id": item_id},
    }


def _stored_summary(conn, source, item_id):
    row = conn.execute(
        "SELECT summary FROM items WHERE source = ? AND item_id = ?", (source, item_id)
    ).fetchone()
    return row[0] if row else None


def test_ai_stores_summary_and_returns_single_output_item(tmp_path, monkeypatch):
    monkeypatch.setenv("ACTIONS_AI_ENABLED", "true")
    monkeypatch.setenv("ACTIONS_AI_PROVIDER", "ollama")
    monkeypatch.setenv("ACTIONS_AI_MAX_CHARS", "0")
    monkeypatch.setattr(actions.ai, "_call_ollama", lambda prompt, model, host: "A summary.")
    conn = get_connection(tmp_path / "radiobeacon.db")
    _insert_item(conn, "senapred", "1", "Some contents.")

    outputs = AiAction().run(_dispatched_event("senapred", "1"), conn=conn)

    assert outputs == [
        {
            "source": "senapred",
            "item_id": "1",
            "summarized": True,
            "summary": "A summary.",
            "provider": "ollama",
            "model": "llama3.2:1b",
        }
    ]
    assert _stored_summary(conn, "senapred", "1") == "A summary."


def test_ai_still_publishes_summarized_false_when_disabled(tmp_path, monkeypatch):
    """Disabled is the DEFAULT out-of-the-box state -- ai must still
    publish (with summarized=False) so actions.chunk, which now
    subscribes to ai's output rather than item.dispatched directly,
    fires even when AI is off."""
    monkeypatch.delenv("ACTIONS_AI_ENABLED", raising=False)
    monkeypatch.setenv("ACTIONS_AI_PROVIDER", "ollama")
    called = []
    monkeypatch.setattr(
        actions.ai, "_call_ollama", lambda prompt, model, host: called.append(1) or "x"
    )
    conn = get_connection(tmp_path / "radiobeacon.db")
    _insert_item(conn, "senapred", "1", "Some contents.")

    outputs = AiAction().run(_dispatched_event("senapred", "1"), conn=conn)

    assert outputs == [
        {
            "source": "senapred",
            "item_id": "1",
            "summarized": False,
            "reason": "ACTIONS_AI_ENABLED is not true",
        }
    ]
    assert called == []
    assert _stored_summary(conn, "senapred", "1") == "Some contents."

    monkeypatch.setenv("ACTIONS_AI_ENABLED", "false")
    outputs = AiAction().run(_dispatched_event("senapred", "1"), conn=conn)
    assert outputs[0]["summarized"] is False
    assert outputs[0]["reason"] == "ACTIONS_AI_ENABLED is not true"
    assert called == []


def test_ai_stores_provider_output_verbatim_without_truncation(tmp_path, monkeypatch):
    monkeypatch.setenv("ACTIONS_AI_ENABLED", "true")
    monkeypatch.setenv("ACTIONS_AI_PROVIDER", "ollama")
    monkeypatch.setenv("ACTIONS_AI_MAX_CHARS", "0")
    long_summary = "A fairly long, complete summary sentence. " * 20
    monkeypatch.setattr(actions.ai, "_call_ollama", lambda prompt, model, host: long_summary)
    conn = get_connection(tmp_path / "radiobeacon.db")
    _insert_item(conn, "senapred", "1", "Some contents.")

    outputs = AiAction().run(_dispatched_event("senapred", "1"), conn=conn)

    # No length-based truncation — the model controls length via the
    # prompt itself, not a hard cut that could chop a sentence in half.
    assert outputs[0]["summarized"] is True
    assert outputs[0]["summary"] == long_summary.strip()
    assert _stored_summary(conn, "senapred", "1") == long_summary.strip()


def test_ai_strips_surrounding_whitespace(tmp_path, monkeypatch):
    monkeypatch.setenv("ACTIONS_AI_ENABLED", "true")
    monkeypatch.setenv("ACTIONS_AI_PROVIDER", "ollama")
    monkeypatch.setenv("ACTIONS_AI_MAX_CHARS", "0")
    monkeypatch.setattr(
        actions.ai, "_call_ollama", lambda prompt, model, host: "  A summary.  \n"
    )
    conn = get_connection(tmp_path / "radiobeacon.db")
    _insert_item(conn, "senapred", "1", "Some contents.")

    outputs = AiAction().run(_dispatched_event("senapred", "1"), conn=conn)

    assert outputs[0]["summary"] == "A summary."


def test_ai_skips_when_extracted_contents_is_null(tmp_path, monkeypatch):
    monkeypatch.setenv("ACTIONS_AI_ENABLED", "true")
    monkeypatch.setenv("ACTIONS_AI_PROVIDER", "ollama")
    called = []
    monkeypatch.setattr(
        actions.ai, "_call_ollama", lambda prompt, model, host: called.append(1) or "x"
    )
    conn = get_connection(tmp_path / "radiobeacon.db")
    _insert_item(conn, "senapred", "1", None)

    outputs = AiAction().run(_dispatched_event("senapred", "1"), conn=conn)

    assert outputs == [
        {
            "source": "senapred",
            "item_id": "1",
            "summarized": False,
            "reason": "item has no extracted_contents",
        }
    ]
    assert called == []
    assert _stored_summary(conn, "senapred", "1") is None


def test_ai_skips_when_extracted_contents_already_within_max_chars(tmp_path, monkeypatch):
    monkeypatch.setenv("ACTIONS_AI_ENABLED", "true")
    monkeypatch.setenv("ACTIONS_AI_PROVIDER", "ollama")
    monkeypatch.setenv("ACTIONS_AI_MAX_CHARS", "500")
    called = []
    monkeypatch.setattr(
        actions.ai, "_call_ollama", lambda prompt, model, host: called.append(1) or "x"
    )
    conn = get_connection(tmp_path / "radiobeacon.db")
    _insert_item(conn, "senapred", "1", "Short contents, well under the limit.")

    outputs = AiAction().run(_dispatched_event("senapred", "1"), conn=conn)

    assert outputs == [
        {
            "source": "senapred",
            "item_id": "1",
            "summarized": False,
            "reason": "extracted_contents already <= 500 chars, nothing to summarize",
        }
    ]
    assert called == []
    assert _stored_summary(conn, "senapred", "1") == "Short contents, well under the limit."


def test_ai_skips_when_item_not_found(tmp_path, monkeypatch):
    monkeypatch.setenv("ACTIONS_AI_ENABLED", "true")
    monkeypatch.setenv("ACTIONS_AI_PROVIDER", "ollama")
    called = []
    monkeypatch.setattr(
        actions.ai, "_call_ollama", lambda prompt, model, host: called.append(1) or "x"
    )
    conn = get_connection(tmp_path / "radiobeacon.db")

    outputs = AiAction().run(_dispatched_event("senapred", "does-not-exist"), conn=conn)

    assert outputs == [
        {
            "source": "senapred",
            "item_id": "does-not-exist",
            "summarized": False,
            "reason": "item not found in items table",
        }
    ]
    assert called == []


def test_ai_skips_when_event_missing_source_or_item_id(tmp_path, monkeypatch):
    monkeypatch.setenv("ACTIONS_AI_ENABLED", "true")
    monkeypatch.setenv("ACTIONS_AI_PROVIDER", "ollama")
    conn = get_connection(tmp_path / "radiobeacon.db")

    outputs = AiAction().run({"data": {}}, conn=conn)

    assert outputs == []


@pytest.mark.parametrize("provider", [None, "bogus"])
def test_ai_skips_when_provider_env_var_unset_or_invalid(tmp_path, monkeypatch, provider):
    monkeypatch.setenv("ACTIONS_AI_ENABLED", "true")
    monkeypatch.setenv("ACTIONS_AI_MAX_CHARS", "0")
    if provider is None:
        monkeypatch.delenv("ACTIONS_AI_PROVIDER", raising=False)
    else:
        monkeypatch.setenv("ACTIONS_AI_PROVIDER", provider)
    conn = get_connection(tmp_path / "radiobeacon.db")
    _insert_item(conn, "senapred", "1", "Some contents.")

    outputs = AiAction().run(_dispatched_event("senapred", "1"), conn=conn)

    assert outputs[0]["summarized"] is False
    assert outputs[0]["reason"].startswith("ACTIONS_AI_PROVIDER=")
    assert _stored_summary(conn, "senapred", "1") == "Some contents."


def test_ai_propagates_provider_call_errors(tmp_path, monkeypatch):
    monkeypatch.setenv("ACTIONS_AI_ENABLED", "true")
    monkeypatch.setenv("ACTIONS_AI_PROVIDER", "ollama")
    monkeypatch.setenv("ACTIONS_AI_MAX_CHARS", "0")

    def _raise(prompt, model, host):
        raise RuntimeError("boom")

    monkeypatch.setattr(actions.ai, "_call_ollama", _raise)
    conn = get_connection(tmp_path / "radiobeacon.db")
    # csn is seeded without ai_fallback_to_title -> a provider failure still
    # propagates (the default hard-stop).
    _insert_item(conn, "csn", "1", "Some contents.")

    with pytest.raises(RuntimeError, match="boom"):
        AiAction().run(_dispatched_event("csn", "1"), conn=conn)

    assert _stored_summary(conn, "csn", "1") is None


def test_ai_falls_back_to_title_when_provider_fails_and_flag_set(tmp_path, monkeypatch):
    monkeypatch.setenv("ACTIONS_AI_ENABLED", "true")
    monkeypatch.setenv("ACTIONS_AI_PROVIDER", "ollama")
    monkeypatch.setenv("ACTIONS_AI_MAX_CHARS", "0")

    def _raise(prompt, model, host):
        raise RuntimeError("boom")

    monkeypatch.setattr(actions.ai, "_call_ollama", _raise)
    conn = get_connection(tmp_path / "radiobeacon.db")
    set_adapter_instance(
        conn, "alerts", "custom", {"code": "def fetch(config): return []", "ai_fallback_to_title": True}
    )
    _insert_item(conn, "alerts", "1", "Some contents.", extracted_title="A short title")

    outputs = AiAction().run(_dispatched_event("alerts", "1"), conn=conn)

    assert len(outputs) == 1
    assert outputs[0]["summarized"] is False
    assert outputs[0]["reason"].startswith("provider call failed")
    assert outputs[0]["provider"] == "ollama"
    assert _stored_summary(conn, "alerts", "1") == "A short title"


def test_ai_fallback_to_title_uses_contents_when_title_empty(tmp_path, monkeypatch):
    monkeypatch.setenv("ACTIONS_AI_ENABLED", "true")
    monkeypatch.setenv("ACTIONS_AI_PROVIDER", "ollama")
    monkeypatch.setenv("ACTIONS_AI_MAX_CHARS", "0")

    def _raise(prompt, model, host):
        raise RuntimeError("boom")

    monkeypatch.setattr(actions.ai, "_call_ollama", _raise)
    conn = get_connection(tmp_path / "radiobeacon.db")
    set_adapter_instance(
        conn, "alerts", "custom", {"code": "def fetch(config): return []", "ai_fallback_to_title": True}
    )
    _insert_item(conn, "alerts", "1", "Some contents.")  # no extracted_title

    outputs = AiAction().run(_dispatched_event("alerts", "1"), conn=conn)

    assert outputs[0]["summarized"] is False
    assert _stored_summary(conn, "alerts", "1") == "Some contents."


def test_ai_publishes_summarized_false_when_store_summary_finds_no_matching_row(
    tmp_path, monkeypatch
):
    """store_summary returns False when its UPDATE matches no row (e.g. the
    item was deleted/re-keyed between ai's SELECT and this call — a real
    race with a concurrent adapter re-poll). ai must not publish
    summarized=True with a summary that was never actually persisted, or
    chunk (which trusts ai's output and re-reads items.summary itself)
    would silently fall back to extracted_contents while ai's own log
    claims success."""
    monkeypatch.setenv("ACTIONS_AI_ENABLED", "true")
    monkeypatch.setenv("ACTIONS_AI_PROVIDER", "ollama")
    monkeypatch.setenv("ACTIONS_AI_MAX_CHARS", "0")
    monkeypatch.setattr(actions.ai, "_call_ollama", lambda prompt, model, host: "A summary.")
    monkeypatch.setattr(actions.ai, "store_summary", lambda conn, source, item_id, summary: False)
    conn = get_connection(tmp_path / "radiobeacon.db")
    _insert_item(conn, "senapred", "1", "Some contents.")

    outputs = AiAction().run(_dispatched_event("senapred", "1"), conn=conn)

    assert outputs[0]["summarized"] is False
    assert outputs[0]["reason"].startswith("summary produced but store_summary matched no row")
    assert _stored_summary(conn, "senapred", "1") is None


def test_ai_picks_up_settings_row_without_restart(tmp_path, monkeypatch):
    """AiAction re-resolves ACTIONS_AI_ENABLED (and every other setting it
    reads) via get_setting(conn=conn) on every run() call — a DB-stored
    override (set via the /config UI) takes effect on the very next
    message, no process restart needed."""
    monkeypatch.delenv("ACTIONS_AI_ENABLED", raising=False)  # would otherwise skip
    conn = get_connection(tmp_path / "radiobeacon.db")
    set_setting(conn, "ACTIONS_AI_ENABLED", "true")
    set_setting(conn, "ACTIONS_AI_PROVIDER", "ollama")
    set_setting(conn, "ACTIONS_AI_MAX_CHARS", "0")
    monkeypatch.setattr(actions.ai, "_call_ollama", lambda prompt, model, host: "A summary.")
    _insert_item(conn, "senapred", "1", "Some contents.")

    outputs = AiAction().run(_dispatched_event("senapred", "1"), conn=conn)

    assert outputs == [
        {
            "source": "senapred",
            "item_id": "1",
            "summarized": True,
            "summary": "A summary.",
            "provider": "ollama",
            "model": "llama3.2:1b",
        }
    ]


def test_ai_passes_db_stored_openai_api_key_to_provider_call(tmp_path, monkeypatch):
    monkeypatch.setenv("ACTIONS_AI_ENABLED", "true")
    monkeypatch.setenv("ACTIONS_AI_PROVIDER", "openai")
    monkeypatch.setenv("ACTIONS_AI_MAX_CHARS", "0")
    conn = get_connection(tmp_path / "radiobeacon.db")
    set_setting(conn, "OPENAI_API_KEY", "sk-from-db", is_secret=True)
    captured = {}
    monkeypatch.setattr(
        actions.ai,
        "_call_openai",
        lambda prompt, model, api_key: captured.setdefault("api_key", api_key) or "A summary.",
    )
    _insert_item(conn, "senapred", "1", "Some contents.")

    AiAction().run(_dispatched_event("senapred", "1"), conn=conn)

    assert captured["api_key"] == "sk-from-db"


def test_ai_passes_none_api_key_when_no_override_stored(tmp_path, monkeypatch):
    """No DB override and no env var set -> api_key=None, preserving today's
    behavior exactly (the SDK client falls back to its own env read)."""
    monkeypatch.setenv("ACTIONS_AI_ENABLED", "true")
    monkeypatch.setenv("ACTIONS_AI_PROVIDER", "claude")
    monkeypatch.setenv("ACTIONS_AI_MAX_CHARS", "0")
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    conn = get_connection(tmp_path / "radiobeacon.db")
    captured = {}
    monkeypatch.setattr(
        actions.ai,
        "_call_claude",
        lambda prompt, model, api_key: captured.setdefault("api_key", api_key) or "A summary.",
    )
    _insert_item(conn, "senapred", "1", "Some contents.")

    AiAction().run(_dispatched_event("senapred", "1"), conn=conn)

    assert captured["api_key"] is None


def test_ai_prompt_includes_item_fields(tmp_path, monkeypatch):
    monkeypatch.setenv("ACTIONS_AI_ENABLED", "true")
    monkeypatch.setenv("ACTIONS_AI_PROVIDER", "ollama")
    monkeypatch.setenv("ACTIONS_AI_MAX_CHARS", "0")
    captured = {}

    def _fake_call(prompt, model, host):
        captured["prompt"] = prompt
        return "A summary."

    monkeypatch.setattr(actions.ai, "_call_ollama", _fake_call)
    conn = get_connection(tmp_path / "radiobeacon.db")
    _insert_item(
        conn, "senapred", "1", "Contenido de la alerta.", extracted_title="Alerta importante"
    )

    AiAction().run(_dispatched_event("senapred", "1"), conn=conn)

    assert "Alerta importante" in captured["prompt"]
    assert "Contenido de la alerta." in captured["prompt"]


def test_ai_prompt_can_reference_every_mapped_adapter_attribute(tmp_path, monkeypatch):
    """The prompt template isn't limited to title/contents — any item
    column (see actions.ai.PROMPT_ITEM_FIELDS) is available as a
    {placeholder}, so a per-adapter override can build a prompt out of
    event_key, type/subtype, transmit_policy, source_date_time, etc."""
    monkeypatch.setenv("ACTIONS_AI_ENABLED", "true")
    monkeypatch.setenv("ACTIONS_AI_PROVIDER", "ollama")
    monkeypatch.setenv("ACTIONS_AI_MAX_CHARS", "0")
    conn = get_connection(tmp_path / "radiobeacon.db")
    set_adapter_instance(
        conn,
        "senapred",
        "custom",
        {
            "code": "def fetch(config): return []",
            "ai_prompt": (
                "src={source} id={item_id} key={event_key} kind={type}/{subtype} "
                "policy={transmit_policy} when={source_date_time} raw={rawdata} "
                "text={extracted_contents}"
            ),
        },
    )
    conn.execute(
        "INSERT INTO items (source, item_id, extracted_title, extracted_contents, url, "
        "event_key, type, subtype, transmit_policy, source_date_time, fetched_at, rawdata) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            "senapred",
            "abc-1",
            "Alerta",
            "Contenido de la alerta.",
            "https://senapred.cl/alerta/abc-1",
            "abc-1",
            "Alerta",
            "Marejadas",
            "urgent",
            "2026-08-19T12:00:00+00:00",
            "2026-08-19T12:05:00",
            '{"titulo": "Alerta"}',
        ),
    )
    conn.commit()
    captured = _capture_prompt(monkeypatch)

    AiAction().run(_dispatched_event("senapred", "abc-1"), conn=conn)

    assert captured["prompt"] == (
        "src=senapred id=abc-1 key=abc-1 kind=Alerta/Marejadas policy=urgent "
        "when=2026-08-19T12:00:00+00:00 raw={\"titulo\": \"Alerta\"} "
        "text=Contenido de la alerta."
    )


def test_ai_prompt_can_reference_source_display_name_and_url(tmp_path, monkeypatch):
    """{source_name}/{source_url} — the source's human display name and site
    URL from the `sources` table — are available to the prompt too, the
    same placeholders the beacon & chunk templates use."""
    monkeypatch.setenv("ACTIONS_AI_ENABLED", "true")
    monkeypatch.setenv("ACTIONS_AI_PROVIDER", "ollama")
    monkeypatch.setenv("ACTIONS_AI_MAX_CHARS", "0")
    conn = get_connection(tmp_path / "radiobeacon.db")
    conn.execute(
        "INSERT INTO sources (source, display_name, site_url) VALUES (?, ?, ?)",
        ("acme", "ACME Alertas", "https://acme.example"),
    )
    conn.commit()
    set_adapter_instance(
        conn,
        "acme",
        "custom",
        {
            "code": "def fetch(config): return []",
            "ai_prompt": "[{source_name}] ({source_url}) {extracted_contents}",
        },
    )
    captured = _capture_prompt(monkeypatch)
    _insert_item(conn, "acme", "1", "Contenido.")

    AiAction().run(_dispatched_event("acme", "1"), conn=conn)

    assert captured["prompt"] == "[ACME Alertas] (https://acme.example) Contenido."


def test_ai_prompt_source_name_falls_back_to_raw_key_when_source_unmanaged(tmp_path, monkeypatch):
    monkeypatch.setenv("ACTIONS_AI_ENABLED", "true")
    monkeypatch.setenv("ACTIONS_AI_PROVIDER", "ollama")
    monkeypatch.setenv("ACTIONS_AI_MAX_CHARS", "0")
    conn = get_connection(tmp_path / "radiobeacon.db")
    conn.execute("DELETE FROM sources WHERE source = 'no-meta'")
    conn.commit()
    set_adapter_instance(
        conn,
        "no-meta",
        "custom",
        {"code": "def fetch(config): return []", "ai_prompt": "src={source_name} {extracted_contents}"},
    )
    captured = _capture_prompt(monkeypatch)
    _insert_item(conn, "no-meta", "1", "Contenido.")

    AiAction().run(_dispatched_event("no-meta", "1"), conn=conn)

    assert captured["prompt"] == "src=no-meta Contenido."


def test_ai_prompt_unknown_placeholder_renders_blank(tmp_path, monkeypatch):
    monkeypatch.setenv("ACTIONS_AI_ENABLED", "true")
    monkeypatch.setenv("ACTIONS_AI_PROVIDER", "ollama")
    monkeypatch.setenv("ACTIONS_AI_MAX_CHARS", "0")
    conn = get_connection(tmp_path / "radiobeacon.db")
    set_adapter_instance(
        conn,
        "senapred",
        "custom",
        {"code": "def fetch(config): return []", "ai_prompt": "[{nope}] {extracted_contents}"},
    )
    captured = _capture_prompt(monkeypatch)
    _insert_item(conn, "senapred", "1", "Contenido.")

    AiAction().run(_dispatched_event("senapred", "1"), conn=conn)

    assert captured["prompt"] == "[] Contenido."


def _capture_prompt(monkeypatch):
    captured = {}

    def _fake_call(prompt, model, host):
        captured["prompt"] = prompt
        return "A summary."

    monkeypatch.setattr(actions.ai, "_call_ollama", _fake_call)
    return captured


def test_ai_uses_per_adapter_prompt_override_when_set(tmp_path, monkeypatch):
    """An adapter_instances row whose config carries `ai_prompt` overrides
    both the global ACTIONS_AI_PROMPT setting and the built-in default for
    that source only."""
    monkeypatch.setenv("ACTIONS_AI_ENABLED", "true")
    monkeypatch.setenv("ACTIONS_AI_PROVIDER", "ollama")
    monkeypatch.setenv("ACTIONS_AI_MAX_CHARS", "0")
    conn = get_connection(tmp_path / "radiobeacon.db")
    set_setting(conn, "ACTIONS_AI_PROMPT", "GLOBAL PROMPT {extracted_contents}")
    set_adapter_instance(
        conn,
        "senapred",
        "custom",
        {"code": "def fetch(config): return []", "ai_prompt": "SENAPRED-MARKER {extracted_contents}"},
    )
    captured = _capture_prompt(monkeypatch)
    _insert_item(conn, "senapred", "1", "Contenido de la alerta.")

    AiAction().run(_dispatched_event("senapred", "1"), conn=conn)

    assert captured["prompt"] == "SENAPRED-MARKER Contenido de la alerta."


def test_ai_falls_back_to_global_setting_when_adapter_has_no_prompt(tmp_path, monkeypatch):
    monkeypatch.setenv("ACTIONS_AI_ENABLED", "true")
    monkeypatch.setenv("ACTIONS_AI_PROVIDER", "ollama")
    monkeypatch.setenv("ACTIONS_AI_MAX_CHARS", "0")
    conn = get_connection(tmp_path / "radiobeacon.db")
    set_setting(conn, "ACTIONS_AI_PROMPT", "GLOBAL PROMPT {extracted_contents}")
    set_adapter_instance(conn, "senapred", "custom", {"code": "def fetch(config): return []"})
    captured = _capture_prompt(monkeypatch)
    _insert_item(conn, "senapred", "1", "Contenido de la alerta.")

    AiAction().run(_dispatched_event("senapred", "1"), conn=conn)

    assert captured["prompt"] == "GLOBAL PROMPT Contenido de la alerta."


def test_ai_falls_back_to_default_prompt_when_no_adapter_row(tmp_path, monkeypatch):
    monkeypatch.setenv("ACTIONS_AI_ENABLED", "true")
    monkeypatch.setenv("ACTIONS_AI_PROVIDER", "ollama")
    monkeypatch.setenv("ACTIONS_AI_MAX_CHARS", "0")
    monkeypatch.delenv("ACTIONS_AI_PROMPT", raising=False)
    conn = get_connection(tmp_path / "radiobeacon.db")
    captured = _capture_prompt(monkeypatch)
    _insert_item(conn, "no-such-adapter", "1", "Contenido de la alerta.")

    AiAction().run(_dispatched_event("no-such-adapter", "1"), conn=conn)

    assert captured["prompt"] == actions.ai._DEFAULT_PROMPT.format(
        extracted_title="",
        extracted_contents="Contenido de la alerta.",
        url="",
        type="",
        subtype="",
    )


def test_ai_event_omits_prompt_by_default(tmp_path, monkeypatch):
    """provider/model are always on the success event; the rendered prompt
    is not, unless ACTIONS_AI_EVENT_INCLUDE_PROMPT is turned on."""
    monkeypatch.setenv("ACTIONS_AI_ENABLED", "true")
    monkeypatch.setenv("ACTIONS_AI_PROVIDER", "ollama")
    monkeypatch.setenv("ACTIONS_AI_MAX_CHARS", "0")
    monkeypatch.setattr(actions.ai, "_call_ollama", lambda prompt, model, host: "A summary.")
    conn = get_connection(tmp_path / "radiobeacon.db")
    _insert_item(conn, "senapred", "1", "Contenido de la alerta.")

    outputs = AiAction().run(_dispatched_event("senapred", "1"), conn=conn)

    assert outputs[0]["provider"] == "ollama"
    assert outputs[0]["model"] == "llama3.2:1b"
    assert "prompt" not in outputs[0]


def test_ai_event_includes_prompt_when_setting_enabled(tmp_path, monkeypatch):
    monkeypatch.setenv("ACTIONS_AI_ENABLED", "true")
    monkeypatch.setenv("ACTIONS_AI_PROVIDER", "ollama")
    monkeypatch.setenv("ACTIONS_AI_MAX_CHARS", "0")
    monkeypatch.setenv("ACTIONS_AI_EVENT_INCLUDE_PROMPT", "true")
    monkeypatch.setattr(actions.ai, "_call_ollama", lambda prompt, model, host: "A summary.")
    conn = get_connection(tmp_path / "radiobeacon.db")
    _insert_item(conn, "senapred", "1", "Contenido de la alerta.")

    outputs = AiAction().run(_dispatched_event("senapred", "1"), conn=conn)

    assert "Contenido de la alerta." in outputs[0]["prompt"]
