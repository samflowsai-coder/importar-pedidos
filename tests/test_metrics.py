"""Tests for app.observability.metrics and the /metrics endpoint."""
from __future__ import annotations

import uuid
from datetime import datetime
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.observability.metrics import (
    outbox_dead_count,
    outbox_pending_count,
    update_outbox_metrics,
    webhook_received_total,
)
from app.persistence import db, repo


@pytest.fixture
def sqlite_tmp(tmp_path: Path):
    db.set_db_path(tmp_path / "app_state.db")
    db.reset_init_cache()
    db.init()
    yield
    db.set_db_path(None)
    db.reset_init_cache()


@pytest.fixture
def client():
    from app.web.server import app  # noqa: PLC0415

    with TestClient(app, raise_server_exceptions=True) as c:
        yield c


def test_metrics_endpoint_returns_200(client):
    r = client.get("/metrics")
    assert r.status_code == 200


def test_metrics_content_type_is_prometheus(client):
    r = client.get("/metrics")
    # prometheus_client returns 'text/plain; version=0.0.4; charset=utf-8'
    assert "text/plain" in r.headers["content-type"]


def test_metrics_response_contains_expected_names(client):
    r = client.get("/metrics")
    body = r.text
    assert "portal_outbox_pending_total" in body
    assert "portal_outbox_dead_total" in body
    assert "portal_poll_fire_duration_seconds" in body


def test_update_outbox_metrics_sets_gauges(sqlite_tmp):
    # Seed one pending and one dead outbox row.
    iid = str(uuid.uuid4())
    repo.insert_import({
        "id": iid,
        "source_filename": "p.pdf",
        "imported_at": datetime.now().isoformat(timespec="seconds"),
        "snapshot": {"header": {"order_number": "T"}, "items": []},
        "status": "success",
        "portal_status": "sent_to_fire",
        "fire_codigo": 1,
    })
    from app.persistence.db import connect  # noqa: PLC0415

    with connect() as conn:
        conn.execute(
            """INSERT INTO outbox
               (import_id, target, endpoint, payload_json, idempotency_key,
                status, created_at)
               VALUES (?, 'gestor', '/v1/orders', '{}', ?, 'pending', ?)""",
            (iid, str(uuid.uuid4()), datetime.now().isoformat()),
        )
        conn.execute(
            """INSERT INTO outbox
               (import_id, target, endpoint, payload_json, idempotency_key,
                status, created_at)
               VALUES (?, 'gestor', '/v1/orders', '{}', ?, 'dead', ?)""",
            (iid, str(uuid.uuid4()), datetime.now().isoformat()),
        )

    update_outbox_metrics()

    assert outbox_pending_count._value.get() == 1.0
    assert outbox_dead_count._value.get() == 1.0


def test_webhook_counter_increments():
    before = webhook_received_total.labels(provider="gestor")._value.get()
    webhook_received_total.labels(provider="gestor").inc()
    after = webhook_received_total.labels(provider="gestor")._value.get()
    assert after == before + 1
