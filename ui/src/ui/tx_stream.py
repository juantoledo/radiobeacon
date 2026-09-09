"""Reverse-proxy + fan-out for the dashboard's live transmission audio.

An operator points ``UI_DASHBOARD_TX_STREAM_URL`` at an HTTP audio stream
carrying SvxLink's transmit audio (an ffmpeg / Icecast capture of an ALSA
loopback or a PulseAudio monitor -- see
documentation/svxlink-txqueue-SETUP.md). This module opens ONE upstream
connection and fans the bytes out to every dashboard ``<audio>`` listening
on ``GET /dashboard/tx-stream``, so:

  - a plain ``ffmpeg ... -listen 1`` source serves any number of viewers,
  - the stream host/port never leaves the box (bind it to localhost),
  - no mixed-content / CORS problem (same origin as the dashboard),
  - the stream inherits the dashboard's auth.

uvicorn runs a single worker (see ui.__main__), so one process-wide hub is
enough. With multiple workers each holds its own upstream connection --
fine for Icecast, not for ``ffmpeg -listen 1``; documented.
"""
import asyncio
import logging

import httpx

logger = logging.getLogger(__name__)

# ~a few seconds of 48 kbps mp3 in httpx's default raw reads; a browser
# that falls this far behind gets an audio glitch, not unbounded memory.
_QUEUE_MAXSIZE = 256
_CONNECT_TIMEOUT_SECONDS = 10.0
# Keep the upstream connection this long after the last listener leaves, so
# a quick page reload / re-key doesn't churn it.
_IDLE_REAP_SECONDS = 10.0


def _offer(q: "asyncio.Queue", item) -> None:
    """Non-blocking put that drops the oldest item when the queue is full."""
    try:
        q.put_nowait(item)
        return
    except asyncio.QueueFull:
        pass
    try:
        q.get_nowait()
    except asyncio.QueueEmpty:
        pass
    try:
        q.put_nowait(item)
    except asyncio.QueueFull:
        pass


class StreamHub:
    """One upstream connection, N downstream listener queues. A queue gets
    ``None`` pushed to it as the end-of-stream sentinel (upstream closed,
    errored, or the hub was torn down)."""

    def __init__(self) -> None:
        self._lock = asyncio.Lock()
        self._url: str | None = None
        self._listeners: set[asyncio.Queue] = set()
        self._pump: asyncio.Task | None = None
        self._reaper: asyncio.Task | None = None
        self.content_type = "audio/mpeg"

    async def subscribe(self, url: str) -> "asyncio.Queue":
        async with self._lock:
            if url != self._url:
                await self._stop_locked()
                self._url = url
                self.content_type = "audio/mpeg"
            if self._reaper is not None:
                self._reaper.cancel()
                self._reaper = None
            q: asyncio.Queue = asyncio.Queue(maxsize=_QUEUE_MAXSIZE)
            self._listeners.add(q)
            if self._pump is None or self._pump.done():
                self._pump = asyncio.create_task(self._pump_upstream(url))
            return q

    def unsubscribe(self, q: "asyncio.Queue") -> None:
        self._listeners.discard(q)
        if not self._listeners and self._reaper is None:
            try:
                self._reaper = asyncio.create_task(self._reap())
            except RuntimeError:  # no running loop (shutdown) — nothing to reap
                pass

    async def aclose(self) -> None:
        async with self._lock:
            if self._reaper is not None:
                self._reaper.cancel()
                self._reaper = None
            await self._stop_locked()
            self._url = None

    async def _reap(self) -> None:
        try:
            await asyncio.sleep(_IDLE_REAP_SECONDS)
        except asyncio.CancelledError:
            return
        async with self._lock:
            self._reaper = None
            if not self._listeners:
                await self._stop_locked()

    async def _stop_locked(self) -> None:
        pump, self._pump = self._pump, None
        if pump is not None and not pump.done():
            pump.cancel()
            try:
                await pump
            except BaseException:
                pass
        self._broadcast(None)

    def _broadcast(self, item) -> None:
        for q in list(self._listeners):
            _offer(q, item)

    async def _pump_upstream(self, url: str) -> None:
        timeout = httpx.Timeout(_CONNECT_TIMEOUT_SECONDS, read=None)
        try:
            async with httpx.AsyncClient(timeout=timeout, follow_redirects=True) as client:
                async with client.stream("GET", url) as resp:
                    resp.raise_for_status()
                    ct = (resp.headers.get("content-type") or "").split(";")[0].strip()
                    if ct:
                        self.content_type = ct
                    async for chunk in resp.aiter_raw():
                        if not self._listeners:
                            return
                        self._broadcast(chunk)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.warning("tx-stream upstream failed: %s", url, exc_info=True)
        finally:
            self._broadcast(None)


hub = StreamHub()
