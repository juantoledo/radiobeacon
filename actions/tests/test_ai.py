import pytest

from adapters.storage import get_connection

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

    assert outputs == [{"source": "senapred", "item_id": "1", "summary": "A summary."}]
    assert _stored_summary(conn, "senapred", "1") == "A summary."


def test_ai_is_a_noop_when_disabled(tmp_path, monkeypatch):
    monkeypatch.delenv("ACTIONS_AI_ENABLED", raising=False)
    monkeypatch.setenv("ACTIONS_AI_PROVIDER", "ollama")
    called = []
    monkeypatch.setattr(
        actions.ai, "_call_ollama", lambda prompt, model, host: called.append(1) or "x"
    )
    conn = get_connection(tmp_path / "radiobeacon.db")
    _insert_item(conn, "senapred", "1", "Some contents.")

    outputs = AiAction().run(_dispatched_event("senapred", "1"), conn=conn)

    assert outputs == []
    assert called == []
    assert _stored_summary(conn, "senapred", "1") is None

    monkeypatch.setenv("ACTIONS_AI_ENABLED", "false")
    outputs = AiAction().run(_dispatched_event("senapred", "1"), conn=conn)
    assert outputs == []
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

    assert outputs == []
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

    assert outputs == []
    assert called == []
    assert _stored_summary(conn, "senapred", "1") is None


def test_ai_skips_when_item_not_found(tmp_path, monkeypatch):
    monkeypatch.setenv("ACTIONS_AI_ENABLED", "true")
    monkeypatch.setenv("ACTIONS_AI_PROVIDER", "ollama")
    called = []
    monkeypatch.setattr(
        actions.ai, "_call_ollama", lambda prompt, model, host: called.append(1) or "x"
    )
    conn = get_connection(tmp_path / "radiobeacon.db")

    outputs = AiAction().run(_dispatched_event("senapred", "does-not-exist"), conn=conn)

    assert outputs == []
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

    assert outputs == []
    assert _stored_summary(conn, "senapred", "1") is None


def test_ai_propagates_provider_call_errors(tmp_path, monkeypatch):
    monkeypatch.setenv("ACTIONS_AI_ENABLED", "true")
    monkeypatch.setenv("ACTIONS_AI_PROVIDER", "ollama")
    monkeypatch.setenv("ACTIONS_AI_MAX_CHARS", "0")

    def _raise(prompt, model, host):
        raise RuntimeError("boom")

    monkeypatch.setattr(actions.ai, "_call_ollama", _raise)
    conn = get_connection(tmp_path / "radiobeacon.db")
    _insert_item(conn, "senapred", "1", "Some contents.")

    with pytest.raises(RuntimeError, match="boom"):
        AiAction().run(_dispatched_event("senapred", "1"), conn=conn)

    assert _stored_summary(conn, "senapred", "1") is None


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
