"""Bounded, thread-safe, drop-oldest FIFO queue. Two independent instances
(voice_queue, frame_queue) get constructed in __main__.py — both fed by
the MQTT client's background thread (paho's loop_start()), both drained by
the main TDMA-loop thread. __main__.py's TDMA loop re-applies
BEACON_QUEUE_MAX_SIZE via set_maxsize() every tick, so capacity is
live-editable like every other beacon setting — maxsize is NOT fixed at
whatever it was when the queue was constructed.

Backed by collections.deque + a plain lock, not queue.Queue: stdlib
queue.Queue only offers reject-new (put_nowait raising Full) or blocking
semantics out of the box, neither of which is drop-oldest — that needs a
manual popleft() before append() under one lock, which queue.Queue doesn't
expose without subclassing anyway.

Drop-oldest (not reject-new or drop-newest) because a beacon exists to
broadcast *current* state, not a perfect history — a backlog built up
during an extended BEACON_ENABLED=false period should represent the
newest items once re-enabled, not stay stuck forever behind the oldest,
now-stale entry (reject-new's failure mode) or silently discard brand-new
arrivals while stale ones linger at the front (drop-newest's)."""
import threading
from collections import deque
from dataclasses import dataclass
from typing import Generic, TypeVar

T = TypeVar("T")


@dataclass(frozen=True)
class QueueStats:
    size: int
    dropped_total: int


class BoundedDropOldestQueue(Generic[T]):
    def __init__(self, maxsize: int):
        if maxsize <= 0:
            raise ValueError("maxsize must be > 0")
        self._maxsize = maxsize
        self._items: deque[T] = deque()
        self._lock = threading.Lock()
        self._dropped_total = 0

    def set_maxsize(self, maxsize: int) -> None:
        """Adjusts capacity on the already-running queue -- lets
        BEACON_QUEUE_MAX_SIZE be edited via /config and take effect
        immediately, matching every other beacon setting's live-editable
        convention, instead of only applying to a queue constructed fresh
        at the next process restart. If the new size is smaller than the
        current backlog, the oldest excess items are dropped right away
        (counted in dropped_total, same as a normal put()-triggered
        drop) rather than left to overflow lazily on the next put()."""
        if maxsize <= 0:
            raise ValueError("maxsize must be > 0")
        with self._lock:
            self._maxsize = maxsize
            while len(self._items) > self._maxsize:
                self._items.popleft()
                self._dropped_total += 1

    def put(self, item: T) -> bool:
        """Returns True if nothing was dropped, False if the queue was
        already full and the oldest item was evicted to make room."""
        with self._lock:
            dropped = False
            if len(self._items) >= self._maxsize:
                self._items.popleft()
                self._dropped_total += 1
                dropped = True
            self._items.append(item)
            return not dropped

    def get_nowait(self) -> T | None:
        """None means empty — not an error/exception, unlike
        queue.Queue.get_nowait()'s Empty exception, since "nothing queued
        right now" is the normal case every TDMA tick checks for."""
        with self._lock:
            if not self._items:
                return None
            return self._items.popleft()

    def stats(self) -> QueueStats:
        with self._lock:
            return QueueStats(size=len(self._items), dropped_total=self._dropped_total)
