"""ui.tx_stream: the reverse-proxy + fan-out hub for the dashboard's live
transmission audio, and the GET /dashboard/tx-stream route."""
import asyncio

import httpx
import pytest
from adapters.storage import set_setting

from ui import tx_stream


# --- fake upstream (httpx.AsyncClient.stream) ---------------------------------


class _FakeResp:
    def __init__(self, chunks, headers, status):
        self._chunks = chunks
        self.headers = headers
        self._status = status

    def raise_for_status(self):
        if self._status >= 400:
            raise httpx.HTTPStatusError("upstream", request=None, response=None)

    async def aiter_raw(self):
        for c in self._chunks:
            await asyncio.sleep(0)  # hand control back to the loop between chunks
            yield c


class _FakeCtx:
    def __init__(self, obj):
        self._obj = obj

    async def __aenter__(self):
        return self._obj

    async def __aexit__(self, *exc):
        return False


def _install_fake_client(monkeypatch, chunks, *, headers=None, status=200):
    hdrs = headers or {"content-type": "audio/mpeg"}

    class _FakeClient:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        def stream(self, method, url):
            return _FakeCtx(_FakeResp(list(chunks), dict(hdrs), status))

    monkeypatch.setattr(tx_stream.httpx, "AsyncClient", _FakeClient)


async def _drain(q, *, timeout=2.0):
    out = []
    while True:
        item = await asyncio.wait_for(q.get(), timeout=timeout)
        if item is None:
            return out
        out.append(item)


# --- hub unit tests ---------------------------------------------------------


def test_hub_fans_one_upstream_out_to_two_listeners(monkeypatch):
    _install_fake_client(monkeypatch, [b"aa", b"bb", b"cc"])

    async def scenario():
        hub = tx_stream.StreamHub()
        q1 = await hub.subscribe("http://localhost:1/s.mp3")
        q2 = await hub.subscribe("http://localhost:1/s.mp3")
        got1, got2 = await _drain(q1), await _drain(q2)
        await hub.aclose()
        return got1, got2

    got1, got2 = asyncio.run(scenario())
    assert b"".join(got1) == b"aabbcc"
    assert b"".join(got2) == b"aabbcc"


def test_hub_echoes_upstream_content_type(monkeypatch):
    _install_fake_client(monkeypatch, [b"x"], headers={"content-type": "audio/ogg; codecs=opus"})

    async def scenario():
        hub = tx_stream.StreamHub()
        q = await hub.subscribe("http://localhost:1/s")
        await _drain(q)
        ct = hub.content_type
        await hub.aclose()
        return ct

    assert asyncio.run(scenario()) == "audio/ogg"


def test_hub_slow_listener_drops_chunks_not_memory(monkeypatch):
    monkeypatch.setattr(tx_stream, "_QUEUE_MAXSIZE", 4)
    _install_fake_client(monkeypatch, [bytes([i]) for i in range(40)])

    async def scenario():
        hub = tx_stream.StreamHub()
        fast = await hub.subscribe("http://localhost:1/s")
        slow = await hub.subscribe("http://localhost:1/s")  # never drained during pump
        fast_got = await _drain(fast)
        slow_got = await _drain(slow)
        await hub.aclose()
        return fast_got, slow_got

    fast_got, slow_got = asyncio.run(scenario())
    assert len(fast_got) == 40  # the draining listener loses nothing
    assert len(slow_got) <= tx_stream._QUEUE_MAXSIZE  # the stalled one is bounded


def test_hub_upstream_error_ends_the_stream_cleanly(monkeypatch):
    _install_fake_client(monkeypatch, [b"x"], status=503)

    async def scenario():
        hub = tx_stream.StreamHub()
        q = await hub.subscribe("http://localhost:1/s")
        got = await _drain(q)  # returns on the None sentinel, no exception
        await hub.aclose()
        return got

    assert asyncio.run(scenario()) == []


def test_hub_restarts_pump_when_url_changes(monkeypatch):
    _install_fake_client(monkeypatch, [b"one"])

    async def scenario():
        hub = tx_stream.StreamHub()
        q1 = await hub.subscribe("http://localhost:1/a")
        await _drain(q1)
        _install_fake_client(monkeypatch, [b"two"])
        q2 = await hub.subscribe("http://localhost:1/b")
        got = await _drain(q2)
        await hub.aclose()
        return got

    assert b"".join(asyncio.run(scenario())) == b"two"


# --- route tests ----------------------------------------------------------


class _StubHub:
    """Pre-fills a listener queue so the route can be tested without any
    upstream connection or background task."""

    content_type = "audio/mpeg"

    def __init__(self, chunks):
        self._chunks = chunks

    async def subscribe(self, url):
        q = asyncio.Queue()
        for c in self._chunks:
            q.put_nowait(c)
        q.put_nowait(None)
        return q

    def unsubscribe(self, q):
        pass


def test_tx_stream_route_404_when_unconfigured(client):
    assert client.get("/dashboard/tx-stream").status_code == 404


def test_tx_stream_route_404_for_non_http_url(client, conn):
    set_setting(conn, "UI_DASHBOARD_TX_STREAM_URL", "ftp://nope/x")
    assert client.get("/dashboard/tx-stream").status_code == 404


def test_tx_stream_route_proxies_the_stream(client, conn, monkeypatch):
    set_setting(conn, "UI_DASHBOARD_TX_STREAM_URL", "http://127.0.0.1:8123/s.mp3")
    monkeypatch.setattr(tx_stream, "hub", _StubHub([b"RIFF", b"-audio", b"-bytes"]))

    resp = client.get("/dashboard/tx-stream")
    assert resp.status_code == 200
    assert resp.content == b"RIFF-audio-bytes"
    assert resp.headers["content-type"] == "audio/mpeg"
    assert resp.headers["cache-control"] == "no-store"


def test_tx_stream_route_open_to_plain_user(user_client, conn, monkeypatch):
    set_setting(conn, "UI_DASHBOARD_TX_STREAM_URL", "http://127.0.0.1:8123/s.mp3")
    monkeypatch.setattr(tx_stream, "hub", _StubHub([b"x"]))

    assert user_client.get("/dashboard/tx-stream").status_code == 200


def test_dashboard_gates_tx_audio_ui_on_the_setting(client, conn, monkeypatch):
    body = client.get("/").text
    assert "tx-audio.js" not in body and 'id="tx-audio"' not in body

    set_setting(conn, "UI_DASHBOARD_TX_STREAM_URL", "http://127.0.0.1:8123/s.mp3")
    body = client.get("/").text
    assert 'data-stream-src="/dashboard/tx-stream"' in body
    assert 'id="tx-audio-toggle"' in body
