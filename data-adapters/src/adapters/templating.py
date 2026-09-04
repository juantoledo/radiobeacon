"""Shared str.format-template rendering, safe against misconfiguration.

Lives here (not in beacon or actions) for the same reason ax25.py's
max_frame_content_bytes does -- both actions.chunk and beacon.formatters
need it, and no direct import exists between those sibling packages
anywhere in this codebase -- data-adapters is the one package every other
package already imports from."""
import logging
import string

logger = logging.getLogger(__name__)


class _SafeFormatter(string.Formatter):
    """str.format() without the attribute / index access a bare
    `template.format(**kwargs)` allows in a field name.

    Operator-authored templates (voice/frame prefixes, the AI-prompt
    override, watermark text, ...) only ever need plain `{name}`
    placeholders, but `{x.__class__.__init__.__globals__[...]}` /
    `{x[0]}` in the same string would walk straight out of the intended
    value -- and those strings are edited through the (unauthenticated)
    dashboard. `get_field` here refuses any field name containing `.` or
    `[`, so only `{name}` resolves."""

    def get_field(self, field_name, args, kwargs):
        if "." in field_name or "[" in field_name:
            raise ValueError(
                f"placeholder {{{field_name}}} may not use '.' or '[]' access"
            )
        return super().get_field(field_name, args, kwargs)


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
        logger.error("%s: invalid placeholder in template %r: %s", context, template, exc)
        return ""
