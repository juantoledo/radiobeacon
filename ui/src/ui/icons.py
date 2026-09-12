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
    "users": (
        '<path d="M8 11a3.5 3.5 0 1 0 0-7 3.5 3.5 0 0 0 0 7Z"/>'
        '<path d="M2.5 20a5.5 5.5 0 0 1 11 0"/>'
        '<path d="M15.5 5.5a3.2 3.2 0 0 1 0 6.2"/>'
        '<path d="M15 13.2c2.8.3 4.9 2 5.5 4.8"/>'
    ),
    "transfer": (
        '<line x1="8" y1="3" x2="8" y2="15"/><polyline points="4 11 8 15 12 11"/>'
        '<line x1="16" y1="21" x2="16" y2="9"/><polyline points="12 13 16 9 20 13"/>'
    ),
    "refresh": (
        '<polyline points="23 4 23 10 17 10"/>'
        '<polyline points="1 20 1 14 7 14"/>'
        '<path d="M3.51 9a9 9 0 0 1 14.85-3.36L23 10M1 14l4.64 4.36A9 9 0 0 0 20.49 15"/>'
    ),
    "sun": (
        '<circle cx="12" cy="12" r="4.5"/>'
        '<path d="M12 2v2.5M12 19.5V22M4.93 4.93l1.77 1.77M17.3 17.3l1.77 1.77'
        'M2 12h2.5M19.5 12H22M4.93 19.07l1.77-1.77M17.3 6.7l1.77-1.77"/>'
    ),
    "moon": ('<path d="M21 12.8A9 9 0 1 1 11.2 3a7 7 0 0 0 9.8 9.8Z"/>'),
    "monitor": (
        '<rect x="2.5" y="4" width="19" height="13" rx="1.5"/>'
        '<line x1="8" y1="21" x2="16" y2="21"/>'
        '<line x1="12" y1="17" x2="12" y2="21"/>'
    ),
    "globe": (
        '<circle cx="12" cy="12" r="9"/>'
        '<line x1="3" y1="12" x2="21" y2="12"/>'
        '<path d="M12 3a14 14 0 0 1 3.5 9A14 14 0 0 1 12 21a14 14 0 0 1-3.5-9A14 14 0 0 1 12 3Z"/>'
    ),
    "volume-2": (
        '<polygon points="11 5 6 9 2 9 2 15 6 15 11 19 11 5"/>'
        '<path d="M15.5 8.5a5 5 0 0 1 0 7"/>'
        '<path d="M18.5 5.5a9 9 0 0 1 0 13"/>'
    ),
    "volume-x": (
        '<polygon points="11 5 6 9 2 9 2 15 6 15 11 19 11 5"/>'
        '<line x1="22" y1="9" x2="16" y2="15"/>'
        '<line x1="16" y1="9" x2="22" y2="15"/>'
    ),
    "badge": (
        '<rect x="4" y="4" width="16" height="16" rx="2"/>'
        '<path d="M9 4v2h6V4"/>'
        '<circle cx="12" cy="11" r="2.4"/>'
        '<path d="M8 17.5a4 4 0 0 1 8 0"/>'
    ),
    "route": (
        '<circle cx="6" cy="19" r="2.4"/>'
        '<circle cx="18" cy="5" r="2.4"/>'
        '<path d="M8.4 19H14a3.5 3.5 0 0 0 0-7h-4a3.5 3.5 0 0 1 0-7h5.6"/>'
    ),
    "tune": (
        '<line x1="4" y1="8" x2="13" y2="8"/><line x1="17" y1="8" x2="20" y2="8"/>'
        '<circle cx="15" cy="8" r="2"/>'
        '<line x1="4" y1="16" x2="7" y2="16"/><line x1="11" y1="16" x2="20" y2="16"/>'
        '<circle cx="9" cy="16" r="2"/>'
    ),
    "shield-check": (
        '<path d="M12 3l7 3v5c0 5-3 8.2-7 10-4-1.8-7-5-7-10V6l7-3Z"/>'
        '<polyline points="9 12 11.3 14.3 15 10"/>'
    ),
    "slash-circle": (
        '<circle cx="12" cy="12" r="9"/>'
        '<line x1="5.6" y1="5.6" x2="18.4" y2="18.4"/>'
    ),
    # Item-category icons (adapters.categories) — purely decorative markers
    # next to an item's type/subtype, wherever items are displayed.
    "cloud": (
        '<path d="M7 18h11a4 4 0 0 0 .4-8 6 6 0 0 0-11.6 1.6A4.5 4.5 0 0 0 7 18Z"/>'
    ),
    "seismograph": (
        '<line x1="2" y1="14" x2="6" y2="14"/>'
        '<polyline points="6 14 9 6 12 18 15 10 17 14"/>'
        '<line x1="17" y1="14" x2="22" y2="14"/>'
    ),
    "tsunami": (
        '<path d="M2 16c1.5-2 3.5-2 5 0s3.5 2 5 0 3.5-2 5 0 3.5 2 5 0"/>'
        '<path d="M2 20c1.5-2 3.5-2 5 0s3.5 2 5 0 3.5-2 5 0 3.5 2 5 0"/>'
        '<path d="M6 12c0-4 3-7 3-9 1 2 1 3 0 4.5"/>'
    ),
    "satellite": (
        '<rect x="9.5" y="9.5" width="5" height="5" rx="1" transform="rotate(45 12 12)"/>'
        '<line x1="14.8" y1="9.2" x2="19" y2="5"/>'
        '<line x1="9.2" y1="14.8" x2="5" y2="19"/>'
        '<path d="M16 3l2 2M19 6l2 2"/>'
        '<path d="M3 16l2 2M5 19l1.5 1.5"/>'
    ),
    "megaphone": (
        '<path d="M3 10v4a1 1 0 0 0 1 1h2l9 4V5l-9 4H4a1 1 0 0 0-1 1Z"/>'
        '<path d="M18 9a4 4 0 0 1 0 6"/>'
        '<path d="M8 15v4a1.5 1.5 0 0 0 3 0v-3"/>'
    ),
    "tag": (
        '<path d="M11.5 3H5a2 2 0 0 0-2 2v6.5a2 2 0 0 0 .6 1.4l8.5 8.5a2 2 0 0 0 2.8 0l6.5-6.5a2 2 0 0 0 0-2.8l-8.5-8.5a2 2 0 0 0-1.4-.6Z"/>'
        '<circle cx="8.5" cy="8.5" r="1.5"/>'
    ),
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
