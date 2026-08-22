"""Static catalog of the env-var-backed settings the /config section exposes
for editing — presentation metadata only (labels, form widget type,
grouping), never imported outside this package. The actual read/write
happens through adapters.storage.get_setting/set_setting/list_settings,
keyed by the exact env var name each SettingSpec.key names.

Scope is deliberately v1-limited: adapters (senapred, csn), actions (chunk,
ai), dispatcher, MQ infra, and the two provider secrets. UI_* settings and
DISPLAY_TIMEZONE are out of scope for now — ui.config stays env-only, and
DISPLAY_TIMEZONE would naturally join a future "Display"/"General" group
alongside UI_* if that scope is ever added."""
from dataclasses import dataclass, field


@dataclass(frozen=True)
class SettingSpec:
    key: str
    group: str
    label: str
    description: str
    type: str  # "text" | "int" | "float" | "bool" | "select" | "secret"
    default: str | None
    is_secret: bool = False
    choices: tuple[str, ...] = field(default_factory=tuple)
    # advanced=True: changes pub/sub topology (topic/event-type wiring) —
    # a typo here silently breaks a pipeline with no visible error, unlike
    # a numeric threshold. Rendered with extra warning styling and a
    # confirm-before-save prompt in config_group_form.html.
    advanced: bool = False


SETTINGS_CATALOG: list[SettingSpec] = [
    # --- Adapters — General ---
    SettingSpec(
        "ADAPTERS_DEFAULT_INTERVAL_SECONDS",
        "Adapters — General",
        "Default poll interval (s)",
        "Fallback poll interval for any adapter lacking its own "
        "ADAPTERS_<NAME>_INTERVAL_SECONDS.",
        "int",
        "600",
    ),
    # --- Adapters — SENAPRED ---
    SettingSpec(
        "ADAPTERS_SENAPRED_INTERVAL_SECONDS",
        "Adapters — SENAPRED",
        "Poll interval (s)",
        "How often the SENAPRED adapter polls for new alerts.",
        "int",
        "600",
    ),
    SettingSpec(
        "ADAPTERS_SENAPRED_IDENTITY_POOL_ID",
        "Adapters — SENAPRED",
        "Cognito identity pool ID",
        "AWS Cognito Identity Pool ID used for SENAPRED's anonymous-visitor auth.",
        "text",
        "us-east-1:17c696bc-53e1-49a2-991f-f1b65f752fda",
    ),
    SettingSpec(
        "ADAPTERS_SENAPRED_COGNITO_REGION",
        "Adapters — SENAPRED",
        "Cognito region",
        "AWS region for the Cognito identity pool above.",
        "text",
        "us-east-1",
    ),
    SettingSpec(
        "ADAPTERS_SENAPRED_APPSYNC_REGION",
        "Adapters — SENAPRED",
        "AppSync region",
        "AWS region used for AppSync GraphQL SigV4 request signing.",
        "text",
        "us-east-1",
    ),
    SettingSpec(
        "ADAPTERS_SENAPRED_APPSYNC_HOST",
        "Adapters — SENAPRED",
        "AppSync host",
        "AppSync GraphQL API host (no scheme/path).",
        "text",
        "rz2uv7ifxbgflh2bqmp6kmh4le.appsync-api.us-east-1.amazonaws.com",
    ),
    SettingSpec(
        "ADAPTERS_SENAPRED_ALERTA_BASE_URL",
        "Adapters — SENAPRED",
        "Alerta base URL",
        "Base URL used to build public links for 'Alerta'-type items.",
        "text",
        "https://senapred.cl/alerta/",
    ),
    SettingSpec(
        "ADAPTERS_SENAPRED_EVENTO_BASE_URL",
        "Adapters — SENAPRED",
        "Evento base URL",
        "Base URL used to build public links for 'Evento'-type items.",
        "text",
        "https://senapred.cl/evento/",
    ),
    SettingSpec(
        "ADAPTERS_SENAPRED_QUERY_LIMIT",
        "Adapters — SENAPRED",
        "Query limit",
        "Max items fetched per SENAPRED feed call (Alerta + Evento each).",
        "int",
        "20",
    ),
    # --- Adapters — CSN ---
    SettingSpec(
        "ADAPTERS_CSN_INTERVAL_SECONDS",
        "Adapters — CSN",
        "Poll interval (s)",
        "How often the CSN adapter polls for new earthquakes.",
        "int",
        "600",
    ),
    SettingSpec(
        "ADAPTERS_CSN_API_URL",
        "Adapters — CSN",
        "API URL",
        "CSN earthquake API endpoint (unofficial JSON mirror).",
        "text",
        "https://api.gael.cloud/general/public/sismos",
    ),
    SettingSpec(
        "ADAPTERS_CSN_SITE_URL",
        "Adapters — CSN",
        "Site URL",
        "Public site URL linked from every CSN item.",
        "text",
        "https://www.sismologia.cl/",
    ),
    SettingSpec(
        "ADAPTERS_CSN_URGENT_MAGNITUDE_THRESHOLD",
        "Adapters — CSN",
        "Urgent magnitude threshold",
        "Magnitude at/above which a CSN item gets dispatch_policy=\"urgent\" "
        "instead of \"informational\".",
        "float",
        "4.5",
    ),
    SettingSpec(
        "ADAPTERS_CSN_SOURCE_TZ",
        "Adapters — CSN",
        "Source timezone",
        "IANA timezone assumed for CSN's naive timestamps, used to convert to UTC.",
        "text",
        "America/Santiago",
    ),
    # --- Dispatcher ---
    SettingSpec(
        "DISPATCHER_INTERVAL_SECONDS",
        "Dispatcher",
        "Poll interval (s)",
        "How often dispatcher polls radiobeacon.db for new items to dispatch.",
        "int",
        "5",
    ),
    SettingSpec(
        "DISPATCHER_CONSUMER_NAME",
        "Dispatcher",
        "Consumer name",
        "Logical consumer name — its own row in dispatcher_state tracks its progress.",
        "text",
        "log",
    ),
    # --- MQ — Dispatcher ---
    SettingSpec(
        "DISPATCHER_MQ_HOST",
        "MQ — Dispatcher",
        "MQTT host",
        "Broker host for dispatcher's audit-event publishing. Unset disables "
        "publishing entirely.",
        "text",
        None,
    ),
    SettingSpec(
        "DISPATCHER_MQ_PORT",
        "MQ — Dispatcher",
        "MQTT port",
        "Broker port for dispatcher's audit-event publishing.",
        "int",
        "1883",
    ),
    SettingSpec(
        "DISPATCHER_MQ_QOS",
        "MQ — Dispatcher",
        "MQTT QoS",
        "QoS level for dispatcher's published events.",
        "int",
        "1",
    ),
    SettingSpec(
        "DISPATCHER_MQ_CONNECT_TIMEOUT_SECONDS",
        "MQ — Dispatcher",
        "Connect timeout (s)",
        "Timeout for dispatcher's MQTT connect/publish.",
        "int",
        "5",
    ),
    # --- MQ — Actions ---
    SettingSpec(
        "ACTIONS_MQ_HOST",
        "MQ — Actions",
        "MQTT host",
        "Broker host actions subscribe to and publish on.",
        "text",
        "localhost",
    ),
    SettingSpec(
        "ACTIONS_MQ_PORT",
        "MQ — Actions",
        "MQTT port",
        "Broker port for actions.",
        "int",
        "1883",
    ),
    SettingSpec(
        "ACTIONS_MQ_QOS",
        "MQ — Actions",
        "MQTT QoS",
        "QoS level for actions' subscribe/publish.",
        "int",
        "1",
    ),
    SettingSpec(
        "ACTIONS_MQ_RECONNECT_BACKOFF_SECONDS",
        "MQ — Actions",
        "Reconnect backoff (s)",
        "Seconds between actions' MQTT reconnect attempts.",
        "int",
        "5",
    ),
    # --- Actions — Chunk ---
    SettingSpec(
        "ACTIONS_CHUNK_SUBSCRIBE_TOPIC",
        "Actions — Chunk",
        "Subscribe topic",
        "MQTT topic ChunkAction subscribes to. Required — action is skipped if unset.",
        "text",
        "radiobeacon/events/item.dispatched",
        advanced=True,
    ),
    SettingSpec(
        "ACTIONS_CHUNK_OUTPUT_TOPIC",
        "Actions — Chunk",
        "Output topic",
        "MQTT topic ChunkAction publishes item.chunked events to.",
        "text",
        "radiobeacon/events/item.chunked",
        advanced=True,
    ),
    SettingSpec(
        "ACTIONS_CHUNK_OUTPUT_EVENT_TYPE",
        "Actions — Chunk",
        "Output event type",
        "CloudEvent `type` string for ChunkAction's output.",
        "text",
        "item.chunked",
        advanced=True,
    ),
    SettingSpec(
        "ACTIONS_CHUNK_MAX_CHARS",
        "Actions — Chunk",
        "Max chars per chunk",
        "Max characters per chunk (word-boundary-safe). Applies live — no restart needed.",
        "int",
        "200",
    ),
    # --- Actions — AI ---
    SettingSpec(
        "ACTIONS_AI_ENABLED",
        "Actions — AI",
        "Enabled",
        "Enables/disables AiAction entirely. Applies live — no restart needed.",
        "bool",
        "false",
    ),
    SettingSpec(
        "ACTIONS_AI_SUBSCRIBE_TOPIC",
        "Actions — AI",
        "Subscribe topic",
        "MQTT topic AiAction subscribes to.",
        "text",
        "radiobeacon/events/item.dispatched",
        advanced=True,
    ),
    SettingSpec(
        "ACTIONS_AI_OUTPUT_TOPIC",
        "Actions — AI",
        "Output topic",
        "MQTT topic AiAction publishes item.summarized events to.",
        "text",
        "radiobeacon/events/item.summarized",
        advanced=True,
    ),
    SettingSpec(
        "ACTIONS_AI_OUTPUT_EVENT_TYPE",
        "Actions — AI",
        "Output event type",
        "CloudEvent `type` string for AiAction's output.",
        "text",
        "item.summarized",
        advanced=True,
    ),
    SettingSpec(
        "ACTIONS_AI_PROVIDER",
        "Actions — AI",
        "Provider",
        "Which LLM provider to call. Required once AI is enabled.",
        "select",
        None,
        choices=("openai", "claude", "ollama"),
    ),
    SettingSpec(
        "ACTIONS_AI_CLAUDE_MODEL",
        "Actions — AI",
        "Claude model",
        "Model name used when provider=claude.",
        "text",
        "claude-haiku-4-5",
    ),
    SettingSpec(
        "ACTIONS_AI_OPENAI_MODEL",
        "Actions — AI",
        "OpenAI model",
        "Model name used when provider=openai.",
        "text",
        "gpt-4o-mini",
    ),
    SettingSpec(
        "ACTIONS_AI_OLLAMA_MODEL",
        "Actions — AI",
        "Ollama model",
        "Model name used when provider=ollama.",
        "text",
        "llama3.2:1b",
    ),
    SettingSpec(
        "ACTIONS_AI_OLLAMA_HOST",
        "Actions — AI",
        "Ollama host",
        "Ollama server host URL, used when provider=ollama.",
        "text",
        "http://localhost:11434",
    ),
    SettingSpec(
        "ACTIONS_AI_MAX_CHARS",
        "Actions — AI",
        "Max input chars",
        "Skip-summarization threshold on input length — items at or under this "
        "length aren't summarized.",
        "int",
        "500",
    ),
    SettingSpec(
        "ACTIONS_AI_PROMPT",
        "Actions — AI",
        "Prompt template",
        "str.format-style override of the default summarization prompt "
        "(placeholders: {extracted_title}, {type}, {subtype}, {extracted_contents}, {url}).",
        "text",
        None,
    ),
    # --- Secrets ---
    SettingSpec(
        "ANTHROPIC_API_KEY",
        "Secrets",
        "Anthropic API key",
        "Used by AiAction when ACTIONS_AI_PROVIDER=claude. Never displayed once "
        "saved — leave unchanged to keep the current value.",
        "secret",
        None,
        is_secret=True,
    ),
    SettingSpec(
        "OPENAI_API_KEY",
        "Secrets",
        "OpenAI API key",
        "Used by AiAction when ACTIONS_AI_PROVIDER=openai. Never displayed once "
        "saved — leave unchanged to keep the current value.",
        "secret",
        None,
        is_secret=True,
    ),
]

GROUPS: list[str] = list(dict.fromkeys(spec.group for spec in SETTINGS_CATALOG))


def group_slug(group: str) -> str:
    """e.g. "Actions — AI" -> "actions-ai" — used in /config/{group} URLs."""
    return group.lower().replace(" — ", "-").replace(" ", "-")


_SLUG_TO_GROUP: dict[str, str] = {group_slug(g): g for g in GROUPS}


def specs_for_group(slug: str) -> list[SettingSpec]:
    group = _SLUG_TO_GROUP.get(slug)
    if group is None:
        return []
    return [spec for spec in SETTINGS_CATALOG if spec.group == group]
