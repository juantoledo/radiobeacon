"""Pure TDMA timing logic — no I/O, no real sleeping, no hidden time.time()
calls. `now` is always an explicit parameter, so this is fully unit
testable by supplying epoch-second values directly.

Cycle boundaries anchor to `now % total_seconds` (epoch-relative, not
process-start-relative) — every call recomputes from scratch rather than
tracking elapsed state, which is what makes this satisfy CONTEXT.md's own
stated orchestrator design principle: "recalculates against the real clock
every iteration, never accumulates sleeps." It's also why beacon needs no
custom NTP-correction logic of its own (see ntp.py) — if the OS steps or
slews the system clock, the very next call to current_slot() already
reflects it, since nothing here remembers a stale prior timestamp."""
from dataclasses import dataclass
from enum import Enum


class Slot(str, Enum):
    VOICE = "voice"
    GUARD = "guard"
    FRAME = "frame"
    IDLE = "idle"


@dataclass(frozen=True)
class WindowConfig:
    total_seconds: int
    voice_seconds: int
    frame_seconds: int
    guard_seconds: int = 0

    def __post_init__(self) -> None:
        if self.total_seconds <= 0:
            raise ValueError("total_seconds must be > 0")
        if self.voice_seconds < 0 or self.frame_seconds < 0 or self.guard_seconds < 0:
            raise ValueError("voice_seconds/frame_seconds/guard_seconds must be >= 0")
        used = self.voice_seconds + self.guard_seconds + self.frame_seconds
        if used > self.total_seconds:
            raise ValueError(
                f"voice_seconds + guard_seconds + frame_seconds ({used}) "
                f"exceeds total_seconds ({self.total_seconds})"
            )


@dataclass(frozen=True)
class SlotState:
    slot: Slot
    cycle_index: int
    elapsed_in_cycle: float
    remaining_in_slot: float
    cycle_started_at: float


def current_slot(config: WindowConfig, now: float) -> SlotState:
    """Layout within one cycle, in order: VOICE (0..voice_seconds), GUARD
    (voice_seconds..voice_seconds+guard_seconds), FRAME (...+frame_seconds),
    then IDLE for whatever's left of total_seconds (the "silencio/reset"
    tail CONTEXT.md's illustrative timing describes) until the next cycle
    starts at the next multiple of total_seconds."""
    cycle_index, elapsed_in_cycle = divmod(now, config.total_seconds)
    cycle_started_at = now - elapsed_in_cycle

    voice_end = config.voice_seconds
    guard_end = voice_end + config.guard_seconds
    frame_end = guard_end + config.frame_seconds

    if elapsed_in_cycle < voice_end:
        slot = Slot.VOICE
        remaining = voice_end - elapsed_in_cycle
    elif elapsed_in_cycle < guard_end:
        slot = Slot.GUARD
        remaining = guard_end - elapsed_in_cycle
    elif elapsed_in_cycle < frame_end:
        slot = Slot.FRAME
        remaining = frame_end - elapsed_in_cycle
    else:
        slot = Slot.IDLE
        remaining = config.total_seconds - elapsed_in_cycle

    return SlotState(
        slot=slot,
        cycle_index=int(cycle_index),
        elapsed_in_cycle=elapsed_in_cycle,
        remaining_in_slot=remaining,
        cycle_started_at=cycle_started_at,
    )
