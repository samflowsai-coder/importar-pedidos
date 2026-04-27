"""Tests for app.persistence.outbox_repo (durable outbound queue)."""
from __future__ import annotations

import uuid
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from app.persistence import db, outbox_repo, repo


@pytest.fixture
def sqlite_tmp(tmp_path: Path):
    db.set_db_path(tmp_path / "app_state.db")
    db.reset_init_cache()
    db.init()
    yield
    db.set_db_path(None)
    db.reset_init_cache()


def _seed_import() -> str:
    iid = str(uuid.uuid4())
    repo.insert_import({
        "id": iid,
        "source_filename": "p.pdf",
        "imported_at": datetime.now().isoformat(timespec="seconds"),
        "snapshot": {"header": {"order_number": "X"}, "items": []},
        "status": "success",
        "portal_status": "sent_to_fire",
        "fire_codigo": 99,
    })
    return iid


def test_enqueue_creates_pending_row(sqlite_tmp):
    iid = _seed_import()
    row = outbox_repo.enqueue(
        import_id=iid,
        target="gestor",
        endpoint="/v1/orders",
        payload={"foo": "bar"},
        idempotency_key=f"key-{uuid.uuid4()}",
    )
    assert row.status == "pending"
    assert row.attempts == 0
    assert row.payload == {"foo": "bar"}
    assert row.target == "gestor"


def test_enqueue_rejects_duplicate_idempotency_key(sqlite_tmp):
    iid = _seed_import()
    key = f"key-{uuid.uuid4()}"
    outbox_repo.enqueue(
        import_id=iid, target="gestor", endpoint="/v1/orders",
        payload={"a": 1}, idempotency_key=key,
    )
    with pytest.raises(outbox_repo.OutboxDuplicateError):
        outbox_repo.enqueue(
            import_id=iid, target="gestor", endpoint="/v1/orders",
            payload={"a": 2}, idempotency_key=key,
        )


def test_claim_next_returns_oldest_pending(sqlite_tmp):
    iid = _seed_import()
    older = outbox_repo.enqueue(
        import_id=iid, target="gestor", endpoint="/v1/orders",
        payload={"n": 1}, idempotency_key=f"k1-{uuid.uuid4()}",
    )
    outbox_repo.enqueue(
        import_id=iid, target="gestor", endpoint="/v1/orders",
        payload={"n": 2}, idempotency_key=f"k2-{uuid.uuid4()}",
    )
    claimed = outbox_repo.claim_next("gestor")
    assert claimed is not None
    assert claimed.id == older.id


def test_claim_next_skips_future_attempts(sqlite_tmp):
    iid = _seed_import()
    row = outbox_repo.enqueue(
        import_id=iid, target="gestor", endpoint="/v1/orders",
        payload={}, idempotency_key=f"k-{uuid.uuid4()}",
    )
    future = (datetime.now() + timedelta(hours=1)).isoformat(timespec="seconds")
    outbox_repo.mark_failed(row.id, error="transient", next_attempt_at=future)
    assert outbox_repo.claim_next("gestor") is None  # still pending but not due


def test_claim_next_filters_by_target(sqlite_tmp):
    iid = _seed_import()
    outbox_repo.enqueue(
        import_id=iid, target="gestor", endpoint="/v1/orders",
        payload={}, idempotency_key=f"k1-{uuid.uuid4()}",
    )
    outbox_repo.enqueue(
        import_id=iid, target="other", endpoint="/v1/x",
        payload={}, idempotency_key=f"k2-{uuid.uuid4()}",
    )
    only_gestor = outbox_repo.claim_next("gestor")
    only_other = outbox_repo.claim_next("other")
    assert only_gestor.target == "gestor"
    assert only_other.target == "other"


def test_mark_sent_records_response(sqlite_tmp):
    iid = _seed_import()
    row = outbox_repo.enqueue(
        import_id=iid, target="gestor", endpoint="/v1/orders",
        payload={}, idempotency_key=f"k-{uuid.uuid4()}",
    )
    outbox_repo.mark_sent(row.id, response={"id": "gestor-1"})
    refreshed = outbox_repo.get(row.id)
    assert refreshed.status == "sent"
    assert refreshed.response == {"id": "gestor-1"}
    assert refreshed.sent_at is not None


def test_mark_failed_increments_attempts(sqlite_tmp):
    iid = _seed_import()
    row = outbox_repo.enqueue(
        import_id=iid, target="gestor", endpoint="/v1/orders",
        payload={}, idempotency_key=f"k-{uuid.uuid4()}",
    )
    outbox_repo.mark_failed(row.id, error="boom")
    refreshed = outbox_repo.get(row.id)
    assert refreshed.attempts == 1
    assert refreshed.last_error == "boom"
    assert refreshed.status == "pending"  # not dead — will retry


def test_mark_failed_dead_stops_retries(sqlite_tmp):
    iid = _seed_import()
    row = outbox_repo.enqueue(
        import_id=iid, target="gestor", endpoint="/v1/orders",
        payload={}, idempotency_key=f"k-{uuid.uuid4()}",
    )
    outbox_repo.mark_failed(row.id, error="permanent", dead=True)
    refreshed = outbox_repo.get(row.id)
    assert refreshed.status == "dead"
    # Dead rows are not claimable
    assert outbox_repo.claim_next("gestor") is None


def test_outbox_cascade_on_import_delete(sqlite_tmp):
    iid = _seed_import()
    outbox_repo.enqueue(
        import_id=iid, target="gestor", endpoint="/v1/orders",
        payload={}, idempotency_key=f"k-{uuid.uuid4()}",
    )
    with db.connect() as conn:
        conn.execute("DELETE FROM imports WHERE id = ?", (iid,))
    assert outbox_repo.list_for_import(iid) == []


def test_find_by_idempotency_key(sqlite_tmp):
    iid = _seed_import()
    key = f"k-{uuid.uuid4()}"
    enq = outbox_repo.enqueue(
        import_id=iid, target="gestor", endpoint="/v1/orders",
        payload={"a": 1}, idempotency_key=key,
    )
    found = outbox_repo.find_by_idempotency_key(key)
    assert found is not None
    assert found.id == enq.id


def test_trace_id_falls_back_to_contextvar(sqlite_tmp):
    from app.observability.trace import with_trace_id

    iid = _seed_import()
    with with_trace_id("trace-outbox"):
        row = outbox_repo.enqueue(
            import_id=iid, target="gestor", endpoint="/v1/orders",
            payload={}, idempotency_key=f"k-{uuid.uuid4()}",
        )
    assert row.trace_id == "trace-outbox"
