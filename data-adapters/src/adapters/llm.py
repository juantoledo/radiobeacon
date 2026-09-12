"""Shared LLM provider clients — one place both actions.ai (the item
summarizer) and adapters.aiprompt_adapter (the content generator) reach a
model through.

Lives in data-adapters for the same reason templating.py / ax25.py do:
it's the one package every other package already imports from, and no
direct import exists between the actions and data-adapters *adapter*
layers. The three call_* helpers keep their provider SDK import lazy
(inside the function) so importing this module never requires anthropic /
openai / ollama to be installed — a deployment that only uses `claude`
needs only `anthropic`.
"""
import logging

from .storage import get_setting

logger = logging.getLogger(__name__)

VALID_PROVIDERS = ("openai", "claude", "ollama")

_DEFAULT_MODELS = {
    "openai": "gpt-4o-mini",
    "claude": "claude-haiku-4-5",
    "ollama": "llama3.2:1b",
}
_DEFAULT_OLLAMA_HOST = "http://localhost:11434"

# Every call_* helper below runs synchronously on whichever thread invoked
# it -- for actions.ai that's paho-mqtt's own loop_start() network thread,
# which also has to keep servicing that connection's keepalive
# PINGREQ/PINGRESP. Each SDK's own default timeout is otherwise multi-minute
# (or, for ollama, unbounded), so a slow/hung provider could starve the
# keepalive long enough for the broker to drop the connection. This bounds
# the worst case instead of inheriting each SDK's default.
_REQUEST_TIMEOUT_SECONDS = 30


def call_openai(prompt: str, model: str, api_key: str | None) -> str:
    import openai

    client = openai.OpenAI(api_key=api_key, timeout=_REQUEST_TIMEOUT_SECONDS) if api_key \
        else openai.OpenAI(timeout=_REQUEST_TIMEOUT_SECONDS)
    response = client.chat.completions.create(
        model=model,
        messages=[{"role": "user", "content": prompt}],
    )
    return response.choices[0].message.content


def call_claude(prompt: str, model: str, api_key: str | None) -> str:
    import anthropic

    client = anthropic.Anthropic(api_key=api_key, timeout=_REQUEST_TIMEOUT_SECONDS) if api_key \
        else anthropic.Anthropic(timeout=_REQUEST_TIMEOUT_SECONDS)
    response = client.messages.create(
        model=model,
        max_tokens=1024,
        messages=[{"role": "user", "content": prompt}],
    )
    return response.content[0].text


def call_ollama(prompt: str, model: str, host: str) -> str:
    import ollama

    client = ollama.Client(host=host, timeout=_REQUEST_TIMEOUT_SECONDS)
    response = client.chat(model=model, messages=[{"role": "user", "content": prompt}])
    return response["message"]["content"]


def resolve_provider_call(prompt: str, *, conn) -> tuple[str, str, str]:
    """Runs `prompt` against whichever provider the deployment is
    configured for and returns (text, provider, model).

    There are deliberately no per-call overrides: this reads exactly the
    same settings actions.ai's summarizer does — the ACTIONS_AI_PROVIDER
    setting, the provider's ACTIONS_AI_*_MODEL setting (built-in default
    when unset), and the ANTHROPIC_API_KEY / OPENAI_API_KEY setting (None
    when unset — the SDK then reads its own env var). Raises ValueError for
    an unknown provider.
    """
    provider = (get_setting("ACTIONS_AI_PROVIDER", conn=conn) or "").strip()
    if provider not in VALID_PROVIDERS:
        raise ValueError(
            f"provider {provider!r} invalid (must be one of {', '.join(VALID_PROVIDERS)})"
        )

    model = get_setting(
        f"ACTIONS_AI_{provider.upper()}_MODEL", _DEFAULT_MODELS[provider], conn=conn
    )

    if provider == "openai":
        text = call_openai(prompt, model, get_setting("OPENAI_API_KEY", conn=conn))
    elif provider == "claude":
        text = call_claude(prompt, model, get_setting("ANTHROPIC_API_KEY", conn=conn))
    else:
        host = get_setting("ACTIONS_AI_OLLAMA_HOST", _DEFAULT_OLLAMA_HOST, conn=conn)
        text = call_ollama(prompt, model, host)

    return text, provider, model
