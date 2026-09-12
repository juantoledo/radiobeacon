"""Curated, bilingual (en/es) taxonomy for the generic AdapterItem.type /
.subtype fields (see base.AdapterItem) — a broad "type" (e.g. seismology)
paired with a finer "subtype" (e.g. quake).

item.type/.subtype have always been plain free-text TEXT columns with no
enum enforcement (see storage.SCHEMA / store_reading's own docstring), and
that doesn't change here: this module is a display/config-time convenience
layered on top, not a constraint. Any adapter (especially a `custom`
Python-code one, which can't be restricted at the code level) is free to
write a value that isn't one of these keys; callers that resolve a key
through get_category/get_subtype must treat a miss as "unrecognized, fall
back to displaying the raw string" rather than an error.

Lives in data-adapters/, not ui/, because ui/ already depends on
data-adapters/ and never the reverse (see settings_registry.py's docstring
for the general rule) — so ui/ can import CATEGORIES directly with no
hand-synced duplicate. Labels carry both languages directly as plain data
(rather than going through ui/'s translations/*.json + t() machinery)
because this catalog also needs to be usable from custom adapter code and
from data-adapters/ itself, neither of which have access to ui/'s i18n
layer. `icon` fields are plain metadata strings naming an icon registered
in ui.icons — this module renders nothing itself, it just carries the
name.
"""

from dataclasses import dataclass, field


@dataclass(frozen=True)
class Subtype:
    key: str
    label_en: str
    label_es: str
    icon: str | None = None


@dataclass(frozen=True)
class Category:
    key: str
    label_en: str
    label_es: str
    icon: str
    subtypes: tuple[Subtype, ...] = field(default_factory=tuple)


CATEGORIES: tuple[Category, ...] = (
    Category(
        key="meteorology",
        label_en="Meteorology",
        label_es="Meteorología",
        icon="cloud",
        subtypes=(
            Subtype("climate", "Climate", "Clima"),
            Subtype("alert", "Alert", "Alerta"),
            Subtype("forecast", "Forecast", "Pronóstico"),
            Subtype("storm", "Storm", "Tormenta"),
        ),
    ),
    Category(
        key="seismology",
        label_en="Seismology",
        label_es="Sismología",
        icon="seismograph",
        subtypes=(
            Subtype("quake", "Quake", "Sismo"),
            Subtype("tsunami", "Tsunami", "Tsunami", icon="tsunami"),
            Subtype("aftershock", "Aftershock", "Réplica"),
        ),
    ),
    Category(
        key="emergency",
        label_en="Emergency & Civil Protection",
        label_es="Emergencias y Protección Civil",
        icon="alert",
        subtypes=(
            Subtype("alert", "Alert", "Alerta"),
            Subtype("evacuation", "Evacuation", "Evacuación"),
            Subtype("event", "Event", "Evento"),
            Subtype("advisory", "Advisory", "Aviso"),
        ),
    ),
    Category(
        key="astronomy",
        label_en="Astronomy & Space Weather",
        label_es="Astronomía y Clima Espacial",
        icon="satellite",
        subtypes=(
            Subtype("solar_flare", "Solar Flare", "Fulguración Solar"),
            Subtype("aurora", "Aurora", "Aurora"),
            Subtype("propagation", "Propagation", "Propagación"),
            Subtype("eclipse", "Eclipse", "Eclipse"),
        ),
    ),
    Category(
        key="amateur_radio",
        label_en="Amateur Radio",
        label_es="Radioafición",
        icon="radio",
        subtypes=(
            Subtype("band_conditions", "Band Conditions", "Condiciones de Banda"),
            Subtype("contest", "Contest", "Concurso"),
            Subtype("net", "Net", "Red"),
            Subtype("bulletin", "Bulletin", "Boletín"),
        ),
    ),
    Category(
        key="news",
        label_en="News & General",
        label_es="Noticias y General",
        icon="megaphone",
        subtypes=(
            Subtype("announcement", "Announcement", "Anuncio"),
            Subtype("bulletin", "Bulletin", "Boletín"),
        ),
    ),
    Category(
        key="other",
        label_en="Other",
        label_es="Otro",
        icon="tag",
        subtypes=(Subtype("general", "General", "General"),),
    ),
)

_BY_KEY: dict[str, Category] = {c.key: c for c in CATEGORIES}


def get_category(key: str | None) -> Category | None:
    """Returns the Category for `key`, or None if `key` is None/unrecognized
    (e.g. legacy free-text data, or a custom adapter's own value)."""
    if not key:
        return None
    return _BY_KEY.get(key)


def get_subtype(category_key: str | None, subtype_key: str | None) -> Subtype | None:
    """Returns the Subtype for (category_key, subtype_key), or None if either
    is missing/unrecognized."""
    category = get_category(category_key)
    if category is None or not subtype_key:
        return None
    for subtype in category.subtypes:
        if subtype.key == subtype_key:
            return subtype
    return None
