"""Shared str.format-template rendering, safe against misconfiguration.

Lives here (not in beacon or actions) for the same reason ax25.py's
max_frame_content_bytes does -- both actions.chunk and beacon.formatters
need it, and no direct import exists between those sibling packages
anywhere in this codebase -- data-adapters is the one package every other
package already imports from."""
import logging

logger = logging.getLogger(__name__)


def safe_format(template: str, context: str, **kwargs) -> str:
    """str.format() that never raises. A misconfigured template (an
    unknown {placeholder}) logs an error naming which setting broke and
    falls back to "" -- never crashes actions.chunk's clamp estimate, and
    never crashes beacon's transmit loop, which has no per-tick
    catch-all and would otherwise die until restarted."""
    if not template:
        return ""
    try:
        return template.format(**kwargs)
    except (KeyError, IndexError) as exc:
        logger.error("%s: invalid placeholder in template %r: %s", context, template, exc)
        return ""
