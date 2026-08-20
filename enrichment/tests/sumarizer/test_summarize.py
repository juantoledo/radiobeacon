import json
import subprocess
import sys
from pathlib import Path

import enrichment.sumarizer as summarize_module
from enrichment.sumarizer import summarize

_ENRICHMENT_SRC = str(Path(__file__).resolve().parents[2] / "src")
_ENRICHMENT_SUMARIZER_ENV_VARS = (
    "ENRICHMENT_SUMARIZER_CLAUDE_MODEL",
    "ENRICHMENT_SUMARIZER_OPENAI_MODEL",
    "ENRICHMENT_SUMARIZER_PROMPT",
)
_PRINT_CONFIG_SNIPPET = (
    "import json; from enrichment.sumarizer import ("
    "CLAUDE_MODEL, OPENAI_MODEL, PROMPT_TEMPLATE); "
    "print(json.dumps({'CLAUDE_MODEL': CLAUDE_MODEL, "
    "'OPENAI_MODEL': OPENAI_MODEL, 'PROMPT_TEMPLATE': PROMPT_TEMPLATE}))"
)


def _read_config_in_subprocess(env_overrides: dict) -> dict:
    """Fresh interpreter, isolated from this test session's already-imported
    enrichment.summarize module — same reasoning as senapred's config tests."""
    import os as os_module

    env = {k: v for k, v in os_module.environ.items() if k not in _ENRICHMENT_SUMARIZER_ENV_VARS}
    env["PYTHONPATH"] = _ENRICHMENT_SRC
    env.update(env_overrides)

    result = subprocess.run(
        [sys.executable, "-c", _PRINT_CONFIG_SNIPPET],
        env=env,
        capture_output=True,
        text=True,
        timeout=15,
    )
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


def test_summarize_returns_none_for_empty_input():
    assert summarize({"extracted_title": None, "extracted_contents": None}) is None
    assert summarize({"extracted_title": "", "extracted_contents": ""}) is None
    assert summarize({}) is None


def test_summarize_returns_text_unchanged_without_provider(monkeypatch):
    monkeypatch.delenv(summarize_module.PROVIDER_ENV_VAR, raising=False)

    result = summarize({"extracted_title": "Titulo", "extracted_contents": "hola mundo"})

    assert result == "Titulo\n\nhola mundo"


def test_summarize_returns_text_unchanged_for_unknown_provider():
    result = summarize(
        {"extracted_title": "Titulo", "extracted_contents": "hola mundo"},
        provider="not-a-real-provider",
    )

    assert result == "Titulo\n\nhola mundo"


def test_summarize_dispatches_to_claude(monkeypatch):
    calls = []

    def fake_claude(prompt):
        calls.append(prompt)
        return "resumen claude"

    monkeypatch.setattr(summarize_module, "_summarize_claude", fake_claude)

    result = summarize(
        {"extracted_contents": "texto largo"}, sentence_count=2, provider="claude"
    )

    assert result == "resumen claude"
    assert len(calls) == 1
    assert "texto largo" in calls[0]


def test_summarize_dispatches_to_openai(monkeypatch):
    monkeypatch.setattr(
        summarize_module, "_summarize_openai", lambda prompt: "resumen openai"
    )

    result = summarize({"extracted_contents": "texto largo"}, provider="openai")

    assert result == "resumen openai"


def test_summarize_falls_back_to_original_text_on_provider_error(monkeypatch):
    def _raise(prompt):
        raise RuntimeError("api unreachable")

    monkeypatch.setattr(summarize_module, "_summarize_claude", _raise)

    result = summarize({"extracted_contents": "texto original"}, provider="claude")

    assert result == "texto original"


def test_summarize_falls_back_when_prompt_references_missing_column(monkeypatch):
    """A custom ENRICHMENT_SUMARIZER_PROMPT referencing a column this call site didn't
    pass in `fields` must not crash summarize() — it should fall back like
    any other summarization failure."""
    monkeypatch.setattr(summarize_module, "PROMPT_TEMPLATE", "{not_a_real_column} {text}")

    result = summarize({"extracted_contents": "texto original"}, provider="claude")

    assert result == "texto original"


def test_summarize_uses_env_var_when_provider_not_passed(monkeypatch):
    monkeypatch.setenv(summarize_module.PROVIDER_ENV_VAR, "claude")
    monkeypatch.setattr(summarize_module, "_summarize_claude", lambda prompt: "via env")

    assert summarize({"extracted_contents": "algo"}) == "via env"


def test_combine_text_joins_title_and_contents():
    assert summarize_module._combine_text("Titulo", "Contenido") == "Titulo\n\nContenido"
    assert summarize_module._combine_text("Titulo", None) == "Titulo"
    assert summarize_module._combine_text(None, None) is None


def test_prompt_substitutes_default_placeholders():
    prompt = summarize_module._prompt({}, "mi texto", 3)

    assert "mi texto" in prompt
    assert "3" in prompt


def test_prompt_supports_any_column_as_placeholder(monkeypatch):
    """The whole point: any key in `fields` becomes a placeholder, not just
    a fixed hardcoded set — including columns invented for this test."""
    monkeypatch.setattr(
        summarize_module,
        "PROMPT_TEMPLATE",
        "{extracted_title} | {extracted_contents} | {source_date_time} | "
        "{url} | {rawdata} | {sentence_count}",
    )

    prompt = summarize_module._prompt(
        {
            "extracted_title": "Mi titulo",
            "extracted_contents": "Mi contenido",
            "source_date_time": "2026-08-19T12:00:00",
            "url": "https://example.com/1",
            "rawdata": '{"id": "1"}',
        },
        "combined",
        2,
    )

    assert prompt == (
        "Mi titulo | Mi contenido | 2026-08-19T12:00:00 | "
        "https://example.com/1 | {\"id\": \"1\"} | 2"
    )


def test_prompt_defaults_missing_field_values_to_empty_string():
    prompt = summarize_module._prompt(
        {"extracted_title": None, "url": None}, "combined", 1
    )

    assert "combined" in prompt


def test_summarize_passes_all_fields_to_prompt(monkeypatch):
    monkeypatch.setattr(
        summarize_module, "PROMPT_TEMPLATE", "T={extracted_title} D={source_date_time}"
    )
    captured = {}
    monkeypatch.setattr(
        summarize_module,
        "_summarize_claude",
        lambda prompt: captured.setdefault("prompt", prompt) or "ok",
    )

    summarize(
        {
            "extracted_title": "Mi titulo",
            "extracted_contents": "Mi contenido",
            "source_date_time": "2026-08-19T12:00:00",
        },
        provider="claude",
    )

    assert captured["prompt"] == "T=Mi titulo D=2026-08-19T12:00:00"


def test_summarize_claude_passes_configured_model(monkeypatch):
    captured = {}

    class FakeMessages:
        def create(self, **kwargs):
            captured.update(kwargs)

            class Block:
                text = "ok"

            class Response:
                content = [Block()]

            return Response()

    class FakeAnthropic:
        def __init__(self):
            self.messages = FakeMessages()

    monkeypatch.setattr("anthropic.Anthropic", FakeAnthropic)

    summarize_module._summarize_claude("texto")

    assert captured["model"] == summarize_module.CLAUDE_MODEL


def test_config_defaults_when_env_vars_unset():
    config = _read_config_in_subprocess({})

    assert config["CLAUDE_MODEL"] == "claude-haiku-4-5"
    assert config["OPENAI_MODEL"] == "gpt-4o-mini"
    assert config["PROMPT_TEMPLATE"] == summarize_module.DEFAULT_PROMPT_TEMPLATE


def test_config_overridable_via_env_vars():
    config = _read_config_in_subprocess(
        {
            "ENRICHMENT_SUMARIZER_CLAUDE_MODEL": "claude-fake-model",
            "ENRICHMENT_SUMARIZER_OPENAI_MODEL": "gpt-fake-model",
            "ENRICHMENT_SUMARIZER_PROMPT": "Custom: {text} ({sentence_count})",
        }
    )

    assert config["CLAUDE_MODEL"] == "claude-fake-model"
    assert config["OPENAI_MODEL"] == "gpt-fake-model"
    assert config["PROMPT_TEMPLATE"] == "Custom: {text} ({sentence_count})"
