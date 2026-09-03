import pytest

import adapters.llm as llm
from adapters.llm import resolve_provider_call
from adapters.storage import get_connection, set_setting


def test_resolves_claude_provider_model_and_key(tmp_path, monkeypatch):
    conn = get_connection(tmp_path / "radiobeacon.db")
    monkeypatch.setenv("ACTIONS_AI_PROVIDER", "claude")
    set_setting(conn, "ANTHROPIC_API_KEY", "sk-db", is_secret=True)
    captured = {}

    def _fake(prompt, model, api_key):
        captured.update(prompt=prompt, model=model, api_key=api_key)
        return "hi"

    monkeypatch.setattr(llm, "call_claude", _fake)

    text, provider, model = resolve_provider_call("say hi", conn=conn)

    assert (text, provider, model) == ("hi", "claude", "claude-haiku-4-5")
    assert captured == {"prompt": "say hi", "model": "claude-haiku-4-5", "api_key": "sk-db"}


def test_uses_configured_model_setting(tmp_path, monkeypatch):
    conn = get_connection(tmp_path / "radiobeacon.db")
    monkeypatch.setenv("ACTIONS_AI_PROVIDER", "openai")
    set_setting(conn, "ACTIONS_AI_OPENAI_MODEL", "gpt-4o")
    monkeypatch.setattr(llm, "call_openai", lambda p, m, k: f"{m}:{p}")

    text, provider, model = resolve_provider_call("x", conn=conn)

    assert (provider, model, text) == ("openai", "gpt-4o", "gpt-4o:x")


def test_ollama_uses_host_setting(tmp_path, monkeypatch):
    conn = get_connection(tmp_path / "radiobeacon.db")
    monkeypatch.setenv("ACTIONS_AI_PROVIDER", "ollama")
    monkeypatch.setenv("ACTIONS_AI_OLLAMA_HOST", "http://ollama.test:11434")
    seen = {}

    def _fake(prompt, model, host):
        seen.update(host=host, model=model)
        return "ok"

    monkeypatch.setattr(llm, "call_ollama", _fake)

    resolve_provider_call("x", conn=conn)

    assert seen == {"host": "http://ollama.test:11434", "model": "llama3.2:1b"}


def test_invalid_provider_raises(tmp_path, monkeypatch):
    conn = get_connection(tmp_path / "radiobeacon.db")
    monkeypatch.setenv("ACTIONS_AI_PROVIDER", "gemini")

    with pytest.raises(ValueError, match="invalid"):
        resolve_provider_call("x", conn=conn)
