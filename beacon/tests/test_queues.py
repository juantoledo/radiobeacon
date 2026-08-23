import threading

import pytest

from beacon.queues import BoundedDropOldestQueue


def test_maxsize_must_be_positive():
    with pytest.raises(ValueError):
        BoundedDropOldestQueue(0)


def test_get_nowait_returns_none_when_empty():
    q = BoundedDropOldestQueue(3)
    assert q.get_nowait() is None


def test_put_then_get_roundtrips():
    q = BoundedDropOldestQueue(3)
    q.put("a")
    assert q.get_nowait() == "a"
    assert q.get_nowait() is None


def test_fifo_order():
    q = BoundedDropOldestQueue(3)
    q.put("a")
    q.put("b")
    q.put("c")
    assert [q.get_nowait(), q.get_nowait(), q.get_nowait()] == ["a", "b", "c"]


def test_put_returns_true_when_nothing_dropped():
    q = BoundedDropOldestQueue(2)
    assert q.put("a") is True
    assert q.put("b") is True


def test_put_drops_oldest_when_full():
    q = BoundedDropOldestQueue(2)
    q.put("a")
    q.put("b")
    result = q.put("c")  # queue full -- "a" should be evicted

    assert result is False
    assert [q.get_nowait(), q.get_nowait(), q.get_nowait()] == ["b", "c", None]


def test_stats_reports_size_and_dropped_total():
    q = BoundedDropOldestQueue(2)
    q.put("a")
    q.put("b")
    q.put("c")  # drops "a"
    q.put("d")  # drops "b"

    stats = q.stats()
    assert stats.size == 2
    assert stats.dropped_total == 2


def test_concurrent_puts_never_exceed_maxsize():
    q = BoundedDropOldestQueue(5)

    def _producer():
        for i in range(200):
            q.put(i)

    threads = [threading.Thread(target=_producer) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert q.stats().size <= 5
