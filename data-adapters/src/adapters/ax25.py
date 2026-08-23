"""Shared AX.25 UI-frame budget math.

Lives here (not in beacon or actions) because both actions.chunk and
beacon.formatters need it, and no direct import exists between those two
sibling packages anywhere in this codebase -- data-adapters is the one
package every other package already imports from."""

AX25_HARD_LIMIT_BYTES = 256  # ~256-byte AX.25 UI frame payload limit (see CONTEXT.md);
# a protocol-safety net, not a user-facing setting.


def max_frame_content_bytes(*, callsign: str, destination: str, prefix: str = "", suffix: str = "") -> int:
    """How many UTF-8 bytes are left for chunk_text itself once
    "{callsign}>{destination}:{prefix}...{suffix}" is assembled, before
    hitting AX25_HARD_LIMIT_BYTES. Can go negative if callsign/destination/
    prefix/suffix alone already exceed the limit -- callers must clamp to
    a sane floor themselves."""
    overhead = f"{callsign}>{destination}:{prefix}{suffix}".encode("utf-8")
    return AX25_HARD_LIMIT_BYTES - len(overhead)
