"""Small hand-authored outline icon set (Feather/Lucide-style: 24x24
viewBox, currentColor stroke, no fill) used via the `icon()` Jinja global
registered in templating.py. Deliberately minimal — only the icons
actually referenced in templates, not a general-purpose library. No
external font/icon-library dependency, matching the app's no-build-step
constraint."""

_ICONS: dict[str, str] = {
    "dashboard": (
        '<rect x="3" y="3" width="7" height="7" rx="1.5"/>'
        '<rect x="14" y="3" width="7" height="7" rx="1.5"/>'
        '<rect x="3" y="14" width="7" height="7" rx="1.5"/>'
        '<rect x="14" y="14" width="7" height="7" rx="1.5"/>'
    ),
    "beacon": (
        '<line x1="12" y1="9" x2="12" y2="21"/>'
        '<circle cx="12" cy="5" r="2"/>'
        '<path d="M8 9a5 5 0 0 1 8 0"/>'
        '<path d="M5 6a9 9 0 0 1 14 0"/>'
    ),
    "items": (
        '<circle cx="4" cy="6" r="1"/><line x1="8" y1="6" x2="21" y2="6"/>'
        '<circle cx="4" cy="12" r="1"/><line x1="8" y1="12" x2="21" y2="12"/>'
        '<circle cx="4" cy="18" r="1"/><line x1="8" y1="18" x2="21" y2="18"/>'
    ),
    "policies": (
        '<line x1="5" y1="4" x2="5" y2="20"/>'
        '<line x1="12" y1="4" x2="12" y2="20"/>'
        '<line x1="19" y1="4" x2="19" y2="20"/>'
        '<circle cx="5" cy="9" r="2"/>'
        '<circle cx="12" cy="15" r="2"/>'
        '<circle cx="19" cy="7" r="2"/>'
    ),
    "config": (
        '<circle cx="12" cy="12" r="3"/>'
        '<path d="M12 2v3M12 19v3M4.2 4.2l2.1 2.1M17.7 17.7l2.1 2.1'
        'M2 12h3M19 12h3M4.2 19.8l2.1-2.1M17.7 6.3l2.1-2.1"/>'
    ),
    "audit": ('<circle cx="12" cy="12" r="9"/><polyline points="12 7 12 12 15.5 14"/>'),
    "developers": (
        '<rect x="3" y="4" width="18" height="16" rx="2"/>'
        '<polyline points="7 9 10 12 7 15"/>'
        '<line x1="12" y1="15" x2="16" y2="15"/>'
    ),
    "plus-circle": (
        '<circle cx="12" cy="12" r="9"/>'
        '<line x1="12" y1="8" x2="12" y2="16"/>'
        '<line x1="8" y1="12" x2="16" y2="12"/>'
    ),
    "database": (
        '<ellipse cx="12" cy="5" rx="8" ry="3"/>'
        '<path d="M4 5v6c0 1.7 3.6 3 8 3s8-1.3 8-3V5"/>'
        '<path d="M4 11v6c0 1.7 3.6 3 8 3s8-1.3 8-3v-6"/>'
    ),
    "activity": (
        '<line x1="4" y1="14" x2="4" y2="20"/>'
        '<line x1="9" y1="8" x2="9" y2="20"/>'
        '<line x1="14" y1="4" x2="14" y2="20"/>'
        '<line x1="19" y1="10" x2="19" y2="20"/>'
    ),
    "layers": (
        '<polygon points="12 4 20 9 12 14 4 9 12 4"/>'
        '<polyline points="4 13 12 18 20 13"/>'
    ),
    "zap": ('<polygon points="13 2 4 14 11 14 10 22 20 9 13 9 13 2"/>'),
    "lock": (
        '<rect x="4" y="11" width="16" height="10" rx="2"/>'
        '<path d="M8 11V7a4 4 0 0 1 8 0v4"/>'
    ),
    "menu": (
        '<line x1="3" y1="6" x2="21" y2="6"/>'
        '<line x1="3" y1="12" x2="21" y2="12"/>'
        '<line x1="3" y1="18" x2="21" y2="18"/>'
    ),
    "close": ('<line x1="6" y1="6" x2="18" y2="18"/><line x1="18" y1="6" x2="6" y2="18"/>'),
    "arrow-right": ('<line x1="5" y1="12" x2="19" y2="12"/><polyline points="12 5 19 12 12 19"/>'),
    "power": ('<path d="M12 3v9"/><path d="M6.6 6.6a9 9 0 1 0 10.8 0"/>'),
    "check": ('<polyline points="20 6 9 17 4 12"/>'),
    "alert": (
        '<path d="M12 3 2 20h20L12 3Z"/>'
        '<line x1="12" y1="10" x2="12" y2="14"/>'
        '<line x1="12" y1="17.5" x2="12" y2="17.5"/>'
    ),
    "rss": ('<path d="M4 11a9 9 0 0 1 9 9"/><path d="M4 4a16 16 0 0 1 16 16"/><circle cx="5" cy="19" r="1.5"/>'),
    "sparkles": (
        '<path d="M12 3l1.8 4.6L18 9l-4.2 1.4L12 15l-1.8-4.6L6 9l4.2-1.4L12 3Z"/>'
        '<path d="M19 14l.8 2.2L22 17l-2.2.8L19 20l-.8-2.2L16 17l2.2-.8L19 14Z"/>'
    ),
    "clock": ('<circle cx="12" cy="12" r="9"/><polyline points="12 7 12 12 15.5 14"/>'),
    "radio": (
        '<circle cx="12" cy="12" r="2"/>'
        '<path d="M7.8 7.8a6 6 0 0 0 0 8.4M16.2 16.2a6 6 0 0 0 0-8.4"/>'
        '<path d="M5 5a10 10 0 0 0 0 14M19 19a10 10 0 0 0 0-14"/>'
    ),
    "play": ('<polygon points="7 4 20 12 7 20 7 4" fill="currentColor"/>'),
    "stop": ('<rect x="6" y="6" width="12" height="12" rx="2" fill="currentColor"/>'),
}


def render_icon(name: str, size: int = 16) -> str:
    """Renders an <svg> string for `name`, or "" for an unknown name (so a
    typo degrades to nothing rather than a template error)."""
    inner = _ICONS.get(name)
    if inner is None:
        return ""
    return (
        f'<svg class="icon" width="{size}" height="{size}" viewBox="0 0 24 24" '
        'fill="none" stroke="currentColor" stroke-width="1.75" '
        'stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">'
        f"{inner}</svg>"
    )
