"""End-to-end tests for the Gestor webhook route.

Covers HMAC validation, idempotency, correlation (external_id +
gestor_order_id fallback), state machine transitions, and the apontae_order_id
side effect.
"""
from __future__ import annotations

import json
import time
import uuid
from datetime import datetime
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.persistence import db, idempotency_repo, repo
from app.security.hmac_verify import compute_signature
from app.state import EventSource, LifecycleEvent, list_events, transition

WEBHOOK_SECRET = "test-secret-current"
WEBHOOK_PATH = "/api/webhooks/gestor"


@pytest.fixture
def isolated_app(tmp_path: Path, monkeypatch):
    db.set_db_path(tmp_path / "app_state.db")
    db.reset_init_cache()
    db.init()
    monkeypatch.setenv("INPUT_DIR", str(tmp_path / "input"))
    monkeypatch.setenv("OUTPUT_DIR", str(tmp_path / "output"))
    monkeypatch.setenv("WEBHOOK_SECRET_GESTOR", WEBHOOK_SECRET)
    yield
    db.set_db_path(None)
    db.reset_init_cache()


def _seed_in_production(import_id: str | None = None, *, gestor_id: str = "gestor-1") -> str:
    """Seed a pedido that's already at production_status='in_production'.

    Mirrors the real flow: parsed → sent_to_fire → post_to_gestor_requested
    → post_to_gestor_sent (production_status moves none → requested → in_production).
    """
    iid = import_id or str(uuid.uuid4())
    repo.insert_import({
        "id": iid,
        "source_filename": "p.pdf",
        "imported_at": datetime.now().isoformat(timespec="seconds"),
        "snapshot": {"header": {"order_number": "X"}, "items": []},
        "status": "success",
        "portal_status": "parsed",
    })
    repo.update_fire_metadata(iid, fire_codigo=42)
    transition(iid, LifecycleEvent.SEND_TO_FIRE_SUCCEEDED, source=EventSource.PORTAL)
    transition(iid, LifecycleEvent.POST_TO_GESTOR_REQUESTED, source=EventSource.PORTAL)
    repo.set_gestor_order_id(iid, gestor_id)
    transition(iid, LifecycleEvent.POST_TO_GESTOR_SENT, source=EventSource.PORTAL)
    return iid


def _signed_post(client: TestClient, body: dict, *, secret: str = WEBHOOK_SECRET):
    raw = json.dumps(body).encode()
    ts = str(int(time.time()))
    sig = compute_signature(secret, ts, raw)
    return client.post(
        WEBHOOK_PATH,
        content=raw,
        headers={
            "Content-Type": "application/json",
            "X-Signature": sig,
            "X-Timestamp": ts,
        },
    )


# ── Authentication / signature checks ────────────────────────────────────


def test_rejects_missing_signature(isolated_app):
    from app.web.server import app
    c = TestClient(app)
    r = c.post(WEBHOOK_PATH, json={"event_id": "x"})
    assert r.status_code == 401


def test_rejects_bad_signature(isolated_app):
    from app.web.server import app
    c = TestClient(app)
    raw = b'{"event_id": "x"}'
    ts = str(int(time.time()))
    bad = compute_signature("wrong-secret", ts, raw)
    r = c.post(
        WEBHOOK_PATH,
        content=raw,
        headers={"X-Signature": bad, "X-Timestamp": ts, "Content-Type": "application/json"},
    )
    assert r.status_code == 403


def test_rejects_replay_old_timestamp(isolated_app):
    from app.web.server import app
    c = TestClient(app)
    raw = b'{"event_id": "x"}'
    old_ts = str(int(time.time()) - 600)  # 10min ago
    sig = compute_signature(WEBHOOK_SECRET, old_ts, raw)
    r = c.post(
        WEBHOOK_PATH,
        content=raw,
        headers={"X-Signature": sig, "X-Timestamp": old_ts, "Content-Type": "application/json"},
    )
    assert r.status_code == 403


def test_secret_rotation_accepts_previous(isolated_app, monkeypatch):
    """During rotation, X-Signature signed with PREVIOUS secret still accepted."""
    monkeypatch.setenv("WEBHOOK_SECRET_GESTOR", "NEW-secret")
    monkeypatch.setenv("WEBHOOK_SECRET_GESTOR_PREVIOUS", "OLD-secret")
    iid = _seed_in_production(gestor_id="g-rot")
    from app.web.server import app
    c = TestClient(app)
    body = {
        "event_id": "evt-rotation",
        "event_type": "production_update",
        "external_id": iid,
    }
    r = _signed_post(c, body, secret="OLD-secret")
    assert r.status_code == 200, r.text


# ── Schema validation ────────────────────────────────────────────────────


def test_rejects_bad_payload_shape(isolated_app):
    from app.web.server import app
    c = TestClient(app)
    # Missing required event_id + event_type
    r = _signed_post(c, {"external_id": "abc"})
    assert r.status_code == 422


def test_rejects_unknown_event_type_via_validation(isolated_app):
    from app.web.server import app
    c = TestClient(app)
    r = _signed_post(c, {
        "event_id": "evt-unknown",
        "event_type": "production_warped",  # not in enum
        "external_id": "x",
    })
    assert r.status_code == 422


# ── Correlation (external_id vs gestor_order_id fallback) ────────────────


def test_correlates_via_external_id(isolated_app):
    iid = _seed_in_production()
    from app.web.server import app
    c = TestClient(app)
    r = _signed_post(c, {
        "event_id": str(uuid.uuid4()),
        "event_type": "production_update",
        "external_id": iid,
        "payload": {"pares_produzidos": 100},
    })
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["import_id"] == iid


def test_correlates_via_gestor_order_id_fallback(isolated_app):
    iid = _seed_in_production(gestor_id="g-fallback-99")
    from app.web.server import app
    c = TestClient(app)
    r = _signed_post(c, {
        "event_id": str(uuid.uuid4()),
        "event_type": "production_update",
        # external_id deliberately omitted
        "gestor_order_id": "g-fallback-99",
    })
    assert r.status_code == 200, r.text
    assert r.json()["import_id"] == iid


def test_404_when_correlation_unresolvable(isolated_app):
    from app.web.server import app
    c = TestClient(app)
    r = _signed_post(c, {
        "event_id": str(uuid.uuid4()),
        "event_type": "production_update",
        "external_id": "does-not-exist",
        "gestor_order_id": "also-unknown",
    })
    assert r.status_code == 404


# ── Idempotency ──────────────────────────────────────────────────────────


def test_replay_returns_cached_response(isolated_app):
    iid = _seed_in_production()
    from app.web.server import app
    c = TestClient(app)
    body = {
        "event_id": "evt-dup-7",
        "event_type": "production_update",
        "external_id": iid,
    }
    r1 = _signed_post(c, body)
    assert r1.status_code == 200
    state_v1 = repo.get_import(iid)["state_version"]

    r2 = _signed_post(c, body)  # same event_id
    assert r2.status_code == 200
    assert r2.json() == r1.json()  # exact same body
    state_v2 = repo.get_import(iid)["state_version"]
    assert state_v1 == state_v2, "replay must NOT bump state_version"

    # Lifecycle log: only ONE production_update event
    events = [e for e in list_events(iid) if e["event_type"] == "production_update"]
    assert len(events) == 1


# ── State machine integration ────────────────────────────────────────────


def test_production_update_keeps_in_production(isolated_app):
    iid = _seed_in_production()
    from app.web.server import app
    c = TestClient(app)
    r = _signed_post(c, {
        "event_id": str(uuid.uuid4()),
        "event_type": "production_update",
        "external_id": iid,
        "payload": {"pares_produzidos": 250, "lote": "A1"},
    })
    assert r.status_code == 200
    entry = repo.get_import(iid)
    assert entry["production_status"] == "in_production"


def test_production_completed_terminal(isolated_app):
    iid = _seed_in_production()
    from app.web.server import app
    c = TestClient(app)
    r = _signed_post(c, {
        "event_id": str(uuid.uuid4()),
        "event_type": "production_completed",
        "external_id": iid,
    })
    assert r.status_code == 200
    entry = repo.get_import(iid)
    assert entry["production_status"] == "completed"


def test_production_cancelled(isolated_app):
    iid = _seed_in_production()
    from app.web.server import app
    c = TestClient(app)
    r = _signed_post(c, {
        "event_id": str(uuid.uuid4()),
        "event_type": "production_cancelled",
        "external_id": iid,
    })
    assert r.status_code == 200
    entry = repo.get_import(iid)
    assert entry["production_status"] == "production_cancelled"


def test_invalid_transition_returns_409(isolated_app):
    """Apply PRODUCTION_UPDATE on a pedido that's only at production='none'."""
    iid = str(uuid.uuid4())
    repo.insert_import({
        "id": iid,
        "source_filename": "p.pdf",
        "imported_at": datetime.now().isoformat(timespec="seconds"),
        "snapshot": {"header": {"order_number": "X"}, "items": []},
        "status": "success",
        "portal_status": "parsed",
    })
    repo.update_fire_metadata(iid, fire_codigo=1)
    transition(iid, LifecycleEvent.SEND_TO_FIRE_SUCCEEDED, source=EventSource.PORTAL)
    repo.set_gestor_order_id(iid, "g-no-prod")
    # Skip POST_TO_GESTOR_* — pedido is at sent_to_fire / production='none'

    from app.web.server import app
    c = TestClient(app)
    r = _signed_post(c, {
        "event_id": str(uuid.uuid4()),
        "event_type": "production_update",
        "external_id": iid,
    })
    assert r.status_code == 409


# ── Side effects ─────────────────────────────────────────────────────────


def test_apontae_order_id_stamped_on_first_event(isolated_app):
    iid = _seed_in_production()
    from app.web.server import app
    c = TestClient(app)
    r = _signed_post(c, {
        "event_id": str(uuid.uuid4()),
        "event_type": "production_update",
        "external_id": iid,
        "apontae_order_id": "apontae-xyz-42",
    })
    assert r.status_code == 200
    entry = repo.get_import(iid)
    assert entry["apontae_order_id"] == "apontae-xyz-42"


def test_idempotency_record_persists(isolated_app):
    iid = _seed_in_production()
    from app.web.server import app
    c = TestClient(app)
    eid = "evt-persisted-1"
    r = _signed_post(c, {
        "event_id": eid,
        "event_type": "production_update",
        "external_id": iid,
    })
    assert r.status_code == 200
    cached = idempotency_repo.get("gestor", eid)
    assert cached is not None
    assert cached.response_status == 200
    assert cached.import_id == iid


def test_lifecycle_event_recorded_with_gestor_source(isolated_app):
    iid = _seed_in_production()
    from app.web.server import app
    c = TestClient(app)
    r = _signed_post(c, {
        "event_id": str(uuid.uuid4()),
        "event_type": "production_update",
        "external_id": iid,
    })
    assert r.status_code == 200

    events = list_events(iid)
    pu = [e for e in events if e["event_type"] == "production_update"]
    assert len(pu) == 1
    assert pu[0]["source"] == "gestor"
    assert pu[0]["payload"]["webhook_event_id"]
