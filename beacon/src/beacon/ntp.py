"""NTP — observability only, never disciplines the clock.

The OS's own NTP daemon (systemd-timesyncd/chronyd — standard on any
modern Linux, assumed already running) does actual clock discipline; this
module never adjusts anything. beacon.schedule.current_slot() reading
time.time() fresh every tick (recomputing cycle boundaries from scratch,
never accumulating sleeps) is beacon's entire "resync" story — if the OS
steps or slews the clock, the very next tick already reflects it, with no
custom protocol code needed.

check_offset() on top of that is a purely optional, read-only visibility
layer: how far off is the system clock right now, for logging/showing in
the UI — never used to correct anything. Not testable against a real NTP
server in this sandbox (no guaranteed outbound network) — tests mock this
boundary."""
import logging
from dataclasses import dataclass

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class NtpCheckResult:
    checked_at: float
    offset_seconds: float | None
    method: str  # "ntplib" | "unavailable"
    ok: bool
    error: str | None


def check_offset(server: str = "pool.ntp.org", *, timeout: float = 5.0) -> NtpCheckResult:
    """Queries `server` via ntplib (lazy-imported — never a hard
    dependency of the core loop) for the current clock offset. Never
    raises: any failure (network unreachable, ntplib missing, timeout)
    comes back as a not-ok result instead."""
    import time

    checked_at = time.time()
    try:
        import ntplib
    except ImportError:
        logger.warning("ntp: ntplib not installed, skipping offset check")
        return NtpCheckResult(
            checked_at=checked_at, offset_seconds=None, method="unavailable", ok=False,
            error="ntplib not installed",
        )

    try:
        client = ntplib.NTPClient()
        response = client.request(server, timeout=timeout)
        return NtpCheckResult(
            checked_at=checked_at, offset_seconds=response.offset, method="ntplib", ok=True,
            error=None,
        )
    except Exception as exc:
        logger.warning("ntp: offset check against %s failed: %s", server, exc)
        return NtpCheckResult(
            checked_at=checked_at, offset_seconds=None, method="ntplib", ok=False, error=str(exc),
        )
