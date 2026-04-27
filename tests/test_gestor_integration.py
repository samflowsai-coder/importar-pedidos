"""Tests for the Gestor de Produção integration: mapper + client + route."""
from __future__ import annotations

import json
import uuid
from datetime import datetime
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from app.http.client import OutboundClient
from app.http.policies import idempotent_post_policy
from app.integrations.gestor import (
    GestorClient,
    GestorClientError,
    build_gestor_payload,
)
from app.integrations.gestor.schema import GestorOrderRequest
from app.models.order import Order, OrderHeader, OrderItem
from app.persistence import db, outbox_repo, repo

# ── Mapper: Order → GestorOrderRequest ──────────────────────────────────


def test_mapper_basic_shape():
    order = Order(
        header=OrderHeader(
            order_number="PED-1",
            issue_date="15/04/2026",
            customer_name="ACME",
            customer_cnpj="00000000000100",
        ),
        items=[
            OrderItem(
                description="Tenis Esportivo",
                product_code="ABC123",
                ean="7891234567890",
                quantity=10,
                unit_price=99.9,
                delivery_date="20/05/2026",
                delivery_name="Loja Centro",
                delivery_cnpj="11111111000111",
                delivery_ean="9991234567890",
            )
        ],
    )
    req = build_gestor_payload(import_id="imp-1", order=order)
    assert isinstance(req, GestorOrderRequest)
    assert req.external_id == "imp-1"
    assert req.supplier_order_number == "PED-1"
    assert req.issue_date == "2026-04-15"  # ISO conversion
    assert req.customer.name == "ACME"
    assert len(req.items) == 1
    item = req.items[0]
    assert item.external_item_id == "ABC123"
    assert item.delivery_date == "2026-05-20"
    assert item.delivery is not None
    assert item.delivery.cnpj == "11111111000111"


def test_mapper_handles_missing_optional_fields():
    order = Order(
        header=OrderHeader(order_number="X"),
        items=[OrderItem(description="A", quantity=1)],
    )
    req = build_gestor_payload(import_id="imp-2", order=order)
    assert req.issue_date is None
    assert req.customer.name is None
    assert req.items[0].delivery is None
    assert req.items[0].external_item_id == "0"  # falls back to index


def test_mapper_passes_unparseable_date_through():
    """If the date doesn't match any known format, leave as-is for the API
    to reject loudly (better than silent corruption)."""
    order = Order(
        header=OrderHeader(order_number="X", issue_date="lixo-invalido"),
        items=[],
    )
    req = build_gestor_payload(import_id="imp-3", order=order)
    assert req.issue_date == "lixo-invalido"


def test_mapper_metadata_forwarded():
    order = Order(header=OrderHeader(order_number="X"), items=[])
    req = build_gestor_payload(
        import_id="imp-4", order=order,
        metadata={"fire_codigo": 42, "trace_id": "t-1"},
    )
    assert req.metadata == {"fire_codigo": 42, "trace_id": "t-1"}


# ── GestorClient over OutboundClient (mock transport) ───────────────────


def _mock_outbound(handler) -> OutboundClient:
    return OutboundClient(
        base_url="http://test",
        retry_policy=idempotent_post_policy(),
        default_headers={"Content-Type": "application/json"},
        transport=httpx.MockTransport(handler),
    )


def test_client_create_order_success():
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["headers"] = dict(request.headers)
        seen["body"] = json.loads(request.content)
        return httpx.Response(201, json={
            "id": "gestor-789", "external_id": "imp-1",
            "status": "received", "received_at": "2026-04-26T10:00:00Z",
        })

    client = GestorClient(api_key="k", outbound=_mock_outbound(handler))
    req = GestorOrderRequest(external_id="imp-1")
    resp = client.create_order(req, idempotency_key="idem-1")

    assert resp.id == "gestor-789"
    assert seen["headers"]["authorization"] == "Bearer k"
    assert seen["headers"]["idempotency-key"] == "idem-1"
    assert seen["body"]["external_id"] == "imp-1"


def test_client_raises_on_4xx():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(400, json={"error": "bad payload"})

    client = GestorClient(api_key="k", outbound=_mock_outbound(handler))
    with pytest.raises(GestorClientError) as ei:
        client.create_order(GestorOrderRequest(external_id="x"), idempotency_key="i")
    assert ei.value.status_code == 400


def test_client_raises_on_schema_mismatch():
    """Wire-format drift surfaces as a clear error, not a silent KeyError."""
    def handler(request: httpx.Request) -> httpx.Response:
        # Missing required `id` field per schema
        return httpx.Response(200, json={"received_at": "now"})

    client = GestorClient(api_key="k", outbound=_mock_outbound(handler))
    with pytest.raises(GestorClientError, match="schema validation"):
        client.create_order(GestorOrderRequest(external_id="x"), idempotency_key="i")


def test_client_raises_when_api_key_missing():
    client = GestorClient(api_key=None, outbound=_mock_outbound(
        lambda r: httpx.Response(200, json={})
    ))
    with pytest.raises(GestorClientError, match="API_KEY"):
        client.create_order(GestorOrderRequest(external_id="x"), idempotency_key="i")


# ── Route: POST /api/imported/{id}/post-to-gestor ───────────────────────

@pytest.fixture
def isolated_app(tmp_path: Path, monkeypatch):
    db.set_db_path(tmp_path / "app_state.db")
    db.reset_init_cache()
    db.init()
    # Steer config away from real folders
    monkeypatch.setenv("INPUT_DIR", str(tmp_path / "input"))
    monkeypatch.setenv("OUTPUT_DIR", str(tmp_path / "output"))
    yield
    db.set_db_path(None)
    db.reset_init_cache()


def _seed_sent_to_fire(import_id: str | None = None) -> str:
    iid = import_id or str(uuid.uuid4())
    # Seed at portal_status='parsed' (no fire_codigo yet) so the SM can move
    # the row through SEND_TO_FIRE_SUCCEEDED, which mirrors real-life flow.
    repo.insert_import({
        "id": iid,
        "source_filename": "p.pdf",
        "imported_at": datetime.now().isoformat(timespec="seconds"),
        "order_number": "PED-1",
        "customer": "ACME",
        "customer_cnpj": "00000000000100",
        "snapshot": {
            "header": {
                "order_number": "PED-1",
                "customer_name": "ACME",
                "customer_cnpj": "00000000000100",
                "issue_date": "15/04/2026",
            },
            "items": [{"description": "Tenis", "quantity": 5, "unit_price": 99.0}],
            "source_file": "",
        },
        "status": "success",
        "portal_status": "parsed",
    })
    # Apply fire_codigo as aux metadata, then transition.
    repo.update_fire_metadata(iid, fire_codigo=1234, sent_to_fire_at="2026-04-26T10:00:00")
    from app.state import EventSource, LifecycleEvent, transition
    transition(iid, LifecycleEvent.SEND_TO_FIRE_SUCCEEDED, source=EventSource.PORTAL)
    return iid


def test_post_to_gestor_happy_path(isolated_app, monkeypatch):
    """End-to-end: enqueue outbox + transition + drain inline + mark sent."""
    iid = _seed_sent_to_fire()

    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["body"] = json.loads(request.content)
        captured["headers"] = dict(request.headers)
        return httpx.Response(200, json={
            "id": "gestor-abc", "external_id": iid, "status": "received",
        })

    # Inject a GestorClient that uses our mock transport, into the route's
    # construction path. Cleanest: monkeypatch GestorClient.__init__ so the
    # route can `GestorClient()` while still hitting our handler.
    from app.integrations.gestor import client as gestor_client_mod
    real_init = gestor_client_mod.GestorClient.__init__

    def patched_init(self, *args, **kwargs):
        kwargs.setdefault("api_key", "test-key")
        kwargs.setdefault("outbound", _mock_outbound(handler))
        real_init(self, *args, **kwargs)

    monkeypatch.setattr(gestor_client_mod.GestorClient, "__init__", patched_init)

    from app.web.server import app
    client = TestClient(app)

    r = client.post(f"/api/imported/{iid}/post-to-gestor")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["gestor_order_id"] == "gestor-abc"
    assert body["production_status"] == "in_production"
    assert body["trace_id"]

    # Outbox row marked sent
    rows = outbox_repo.list_for_import(iid)
    assert len(rows) == 1
    assert rows[0].status == "sent"
    assert rows[0].response["id"] == "gestor-abc"
    assert rows[0].response["external_id"] == iid
    assert rows[0].response["status"] == "received"

    # Imports row updated with gestor_order_id
    entry = repo.get_import(iid)
    assert entry["gestor_order_id"] == "gestor-abc"
    assert entry["production_status"] == "in_production"

    # Lifecycle log: REQUESTED → SENT
    from app.state import list_events
    events = [e["event_type"] for e in list_events(iid)]
    assert "post_to_gestor_requested" in events
    assert "post_to_gestor_sent" in events

    # Idempotency-Key reached the wire
    assert "idempotency-key" in captured["headers"]


def test_post_to_gestor_rejects_when_not_in_fire(isolated_app):
    """Pedido em portal_status='parsed' não pode ir pro Gestor."""
    iid = str(uuid.uuid4())
    repo.insert_import({
        "id": iid,
        "source_filename": "p.pdf",
        "imported_at": datetime.now().isoformat(timespec="seconds"),
        "snapshot": {"header": {"order_number": "X"}, "items": []},
        "status": "success",
        "portal_status": "parsed",
    })

    from app.web.server import app
    client = TestClient(app)
    r = client.post(f"/api/imported/{iid}/post-to-gestor")
    assert r.status_code == 409
    assert "Fire" in r.json()["detail"]


def test_post_to_gestor_rejects_when_already_requested(isolated_app, monkeypatch):
    """Second POST should fail because production_status != 'none'."""
    iid = _seed_sent_to_fire()

    def handler(request):
        return httpx.Response(200, json={"id": "g-1", "external_id": iid})

    from app.integrations.gestor import client as gestor_client_mod
    real_init = gestor_client_mod.GestorClient.__init__
    def patched_init(self, *a, **kw):
        kw.setdefault("api_key", "k")
        kw.setdefault("outbound", _mock_outbound(handler))
        real_init(self, *a, **kw)
    monkeypatch.setattr(gestor_client_mod.GestorClient, "__init__", patched_init)

    from app.web.server import app
    c = TestClient(app)
    r1 = c.post(f"/api/imported/{iid}/post-to-gestor")
    assert r1.status_code == 200, r1.text
    r2 = c.post(f"/api/imported/{iid}/post-to-gestor")
    assert r2.status_code == 409
    assert "já enviado" in r2.json()["detail"]


def test_post_to_gestor_marks_failed_on_4xx(isolated_app, monkeypatch):
    """Gestor rejection: outbox failed + transition POST_TO_GESTOR_FAILED."""
    iid = _seed_sent_to_fire()

    def handler(request):
        return httpx.Response(400, json={"error": "schema"})

    from app.integrations.gestor import client as gestor_client_mod
    real_init = gestor_client_mod.GestorClient.__init__
    def patched_init(self, *a, **kw):
        kw.setdefault("api_key", "k")
        kw.setdefault("outbound", _mock_outbound(handler))
        real_init(self, *a, **kw)
    monkeypatch.setattr(gestor_client_mod.GestorClient, "__init__", patched_init)

    from app.web.server import app
    c = TestClient(app)
    r = c.post(f"/api/imported/{iid}/post-to-gestor")
    assert r.status_code == 502

    # State reverts to REQUESTED (failed transition keeps production_status)
    entry = repo.get_import(iid)
    assert entry["production_status"] == "production_requested"

    # Outbox marked failed (not dead — would be rescheduled by future worker)
    rows = outbox_repo.list_for_import(iid)
    assert rows[0].status == "pending"
    assert rows[0].attempts == 1
    assert rows[0].last_error  # populated

    from app.state import list_events
    events = [e["event_type"] for e in list_events(iid)]
    assert "post_to_gestor_failed" in events
