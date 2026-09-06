"""Shared default values for the actions package's settings — single
source of truth for actions.ai (the summarizer), ui.routers.adapters
(shows this as the placeholder in each adapter's "AI prompt override"
box) and config_catalog.py's own SettingSpec default for ACTIONS_AI_PROMPT.

Lives in the adapters package because it's the lowest layer both actions
and ui already depend on — mirrors beacon_defaults.py.
"""

# str.format-style template. It may reference any of an item's mapped
# adapter attributes as a {placeholder} — {source}, {item_id},
# {extracted_title}, {extracted_contents}, {summary}, {url}, {event_key},
# {type}, {subtype}, {policy}, {source_date_time}, {fetched_at},
# {captured_at}, {rawdata} (see actions.ai.PROMPT_ITEM_FIELDS) — plus the
# source's display name / site URL as {source_name} / {source_url} (the
# same placeholders the beacon & chunk templates use). An unknown
# placeholder just renders blank. Used verbatim when neither a per-adapter
# override (adapter_instances.config.ai_prompt) nor the global
# ACTIONS_AI_PROMPT setting is set. This default only uses a handful.
AI_PROMPT_DEFAULT = (
    "Resume el siguiente aviso en español, en 2 a 3 oraciones como máximo, "
    "claras y completas — sin inventar información que no esté presente, "
    "y sin dejar ninguna oración a medias.\n\n"
    "Título: {extracted_title}\n"
    "Tipo: {type} / {subtype}\n"
    "Contenido: {extracted_contents}\n"
    "URL: {url}\n\n"
    "Resumen:"
)
