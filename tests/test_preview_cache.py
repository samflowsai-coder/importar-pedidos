"""Unit tests for PreviewCache (TTL, LRU, consume semantics)."""
from __future__ import annotations

import time

import pytest

from app.models.order import Order, OrderHeader, OrderItem
from app.web.preview_cache import (
    PreviewCache,
    PreviewConsumedError,
    PreviewNotFoundError,
)


def _order(n: int = 1) -> Order:
    return Order(
        header=OrderHeader(order_number=f"PED-{n}", customer_name="ACME"),
        items=[OrderItem(description="Item", quantity=1.0)],
    )


def test_put_and_get_roundtrip():
    cache = PreviewCache()
    entry = cache.put(_order(1), "a.pdf", b"raw", ".pdf")

    got = cache.get(entry.preview_id)
    assert got is not None
    assert got.order.header.order_number == "PED-1"
    assert got.consumed is False
    assert cache.size() == 1


def test_consume_marks_entry_and_rejects_second_consume():
    cache = PreviewCache()
    entry = cache.put(_order(1), "a.pdf", b"raw", ".pdf")

    consumed = cache.consume(entry.preview_id)
    assert consumed.consumed is True
    assert consumed.order.header.order_number == "PED-1"

    with pytest.raises(PreviewConsumedError):
        cache.consume(entry.preview_id)


def test_consume_unknown_raises_not_found():
    cache = PreviewCache()
    with pytest.raises(PreviewNotFoundError):
        cache.consume("does-not-exist")


def test_ttl_expires_entries():
    cache = PreviewCache(ttl_seconds=1)
    entry = cache.put(_order(1), "a.pdf", b"raw", ".pdf")
    assert cache.size() == 1

    time.sleep(1.05)
    assert cache.get(entry.preview_id) is None
    assert cache.size() == 0


def test_lru_evicts_oldest_beyond_cap():
    cache = PreviewCache(max_entries=3)
    ids = [cache.put(_order(i), f"f{i}.pdf", b"", ".pdf").preview_id for i in range(5)]

    # First two should have been evicted; last three remain
    assert cache.get(ids[0]) is None
    assert cache.get(ids[1]) is None
    for pid in ids[2:]:
        assert cache.get(pid) is not None
    assert cache.size() == 3


def test_get_moves_entry_to_mru_end():
    cache = PreviewCache(max_entries=3)
    a = cache.put(_order(1), "a.pdf", b"", ".pdf").preview_id
    b = cache.put(_order(2), "b.pdf", b"", ".pdf").preview_id
    c = cache.put(_order(3), "c.pdf", b"", ".pdf").preview_id

    # Touch `a` so it becomes most-recently-used
    cache.get(a)
    # New insertion should push `b` (now oldest) out
    cache.put(_order(4), "d.pdf", b"", ".pdf")

    assert cache.get(a) is not None
    assert cache.get(b) is None
    assert cache.get(c) is not None


def test_drop_removes_entry():
    cache = PreviewCache()
    entry = cache.put(_order(1), "a.pdf", b"", ".pdf")
    cache.drop(entry.preview_id)
    assert cache.get(entry.preview_id) is None
