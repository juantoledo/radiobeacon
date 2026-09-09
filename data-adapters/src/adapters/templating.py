"""Shared str.format-template rendering, safe against misconfiguration.

Lives here (not in beacon or actions) for the same reason ax25.py's
max_frame_content_bytes does -- both actions.chunk and beacon.formatters
need it, and no direct import exists between those sibling packages
anywhere in this codebase -- data-adapters is the one package every other
package already imports from."""
import logging
import string

logger = logging.getLogger(__name__)


def _unwrap_collection(value):
    """While `value` is a list, replace it with its first element (`None`
    if empty). A mapping template names the record it wants, not which
    index of a collection holds it, so a list encountered anywhere along a
    dotted path -- including as the final resolved value -- is treated as
    "the record I'm looking for is at index 0"."""
    while isinstance(value, list):
        value = value[0] if value else None
    return value


def resolve_dotted_path(root, dotted_path: str):
    """Walks `dotted_path` (e.g. "estacion.nombre") through nested dicts
    only -- never getattr -- so it's safe against `{x.__class__...}`-style
    attacks even though it allows '.'-separated navigation: a path landing
    on anything that isn't a dict simply fails the isinstance check below,
    no dunder-name blocklist required. A list encountered at any step
    (including `root` itself) is collapsed to its first element (see
    _unwrap_collection). A missing key or a non-dict node raises plain
    ValueError -- deliberately the *same* type an unknown flat
    {placeholder} already raises, so safe_format's existing
    `except (KeyError, IndexError, ValueError)` swallows a broken dotted
    path exactly the way it swallows any other misconfigured template,
    with no change to that "never raises" contract for any caller."""
    value = _unwrap_collection(root)
    for part in dotted_path.split("."):
        if not isinstance(value, dict):
            raise ValueError(
                f"cannot look up {part!r} on a {type(value).__name__} (in {dotted_path!r})"
            )
        if part not in value:
            raise ValueError(f"{part!r} not found (in {dotted_path!r})")
        value = _unwrap_collection(value[part])
    return value


def dotted_placeholder_paths(template: str) -> list[tuple[str, str]]:
    """Every {first.rest} placeholder in `template`, as (first, rest) pairs
    -- e.g. "Temp {estacion.medicion.valor}" -> [("estacion", "medicion.valor")].
    Placeholders with no dot are omitted. Reuses string.Formatter's own
    tokenizer so `{{`/`}}` escaping and `!conversion`/`:format_spec`
    suffixes are stripped exactly the way the real render does."""
    pairs = []
    for _, field_name, _, _ in string.Formatter().parse(template):
        if field_name and "." in field_name:
            first, _, rest = field_name.partition(".")
            pairs.append((first, rest))
    return pairs


class _SafeFormatter(string.Formatter):
    """str.format() without the attribute / index access a bare
    `template.format(**kwargs)` allows in a field name.

    Operator-authored templates (voice/frame prefixes, the AI-prompt
    override, watermark text, the API adapter's field mapping, ...) only
    ever need plain `{name}` placeholders or a dotted `{name.nested.path}`
    into a JSON-shaped value (dict/list only), but
    `{x.__class__.__init__.__globals__[...]}` / `{x[0]}` in the same
    string would walk straight out of the intended value -- and these
    strings are edited through the (unauthenticated) dashboard. `get_field`
    here still refuses any field name containing `[`; a `.` is now allowed
    but only ever resolved via `resolve_dotted_path`'s dict-only traversal,
    never `getattr`, so the attribute-walk attack stays blocked without a
    dunder-name blocklist."""

    def get_field(self, field_name, args, kwargs):
        if "[" in field_name:
            raise ValueError(f"placeholder {{{field_name}}} may not use '[]' access")
        first, _, rest = field_name.partition(".")
        obj, used_key = super().get_field(first, args, kwargs)
        if rest:
            obj = resolve_dotted_path(obj, rest)
        return obj, used_key


_FORMATTER = _SafeFormatter()


def safe_format(template: str, context: str, **kwargs) -> str:
    """str.format() that never raises and never lets a template reach past
    its named placeholders. A misconfigured template (an unknown
    {placeholder}, or one using forbidden `.`/`[]` access) logs an error
    naming which setting broke and falls back to "" -- never crashes
    actions.chunk's clamp estimate, and never crashes beacon's transmit
    loop, which has no per-tick catch-all and would otherwise die until
    restarted."""
    if not template:
        return ""
    try:
        return _FORMATTER.vformat(template, (), kwargs)
    except (KeyError, IndexError, ValueError) as exc:
        logger.error(
            "invalid placeholder in template context=%s template=%r reason=%s", context, template, exc
        )
        return ""
