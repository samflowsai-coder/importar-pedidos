"""Tests for the state machine: pure transition table + DB-backed transition()."""
from __future__ import annotations

import random
import uuid
from datetime import datetime
from pathlib import Path

import pytest

from app.persistence import db, repo
from app.state import (
    EventSource,
    InvalidTransitionError,
    LifecycleEvent,
    PortalStatus,
    ProductionStatus,
    list_events,
    replay_state,
    transition,
)
from app.state.events import StaleStateError
from app.state.machine import (
    PORTAL_TRANSITIONS,
    PRODUCTION_TRANSITIONS,
    apply_event,
    is_valid,
)


@pytest.fixture
def sqlite_tmp(tmp_path: Path):
    db.set_db_path(tmp_path / "app_state.db")
    db.reset_init_cache()
    db.init()
    yield
    db.set_db_path(None)
    db.reset_init_cache()


def _seed_parsed(import_id: str | None = None, **overrides) -> str:
    """Insert a fresh row at portal_status='parsed', production_status='none'."""
    iid = import_id or str(uuid.uuid4())
    entry = {
        "id": iid,
        "source_filename": "pedido.pdf",
        "imported_at": datetime.now().isoformat(timespec="seconds"),
        "order_number": "PED-001",
        "customer": "ACME",
        "customer_cnpj": "00000000000100",
        "snapshot": {"header": {"order_number": "PED-001"}, "items": []},
        "status": "success",
        "portal_status": "parsed",
    }
    entry.update(overrides)
    repo.insert_import(entry)
    return iid


# ── Pure transition table (machine.py) ────────────────────────────────────


def test_apply_event_happy_path():
    portal, prod = apply_event(
        PortalStatus.PARSED, ProductionStatus.NONE, LifecycleEvent.SEND_TO_FIRE_SUCCEEDED
    )
    assert portal == PortalStatus.SENT_TO_FIRE
    assert prod == ProductionStatus.NONE


def test_apply_event_rejects_unknown_transition():
    with pytest.raises(InvalidTransitionError):
        apply_event(
            PortalStatus.SENT_TO_FIRE, ProductionStatus.NONE, LifecycleEvent.IMPORTED
        )


def test_send_to_fire_failed_does_not_move_portal():
    portal, prod = apply_event(
        PortalStatus.PARSED, ProductionStatus.NONE, LifecycleEvent.SEND_TO_FIRE_FAILED
    )
    assert portal == PortalStatus.PARSED
    assert prod == ProductionStatus.NONE


def test_cancelled_is_terminal():
    portal, prod = apply_event(
        PortalStatus.PARSED, ProductionStatus.NONE, LifecycleEvent.CANCELLED
    )
    assert portal == PortalStatus.CANCELLED
    # Any subsequent event from CANCELLED state should be invalid (no transitions defined)
    with pytest.raises(InvalidTransitionError):
        apply_event(portal, prod, LifecycleEvent.SEND_TO_FIRE_SUCCEEDED)


def test_post_to_gestor_chain():
    """Phase 3 vocabulary: NONE → REQUESTED → IN_PRODUCTION."""
    p, q = apply_event(
        PortalStatus.SENT_TO_FIRE, ProductionStatus.NONE,
        LifecycleEvent.POST_TO_GESTOR_REQUESTED,
    )
    assert q == ProductionStatus.REQUESTED
    p, q = apply_event(p, q, LifecycleEvent.POST_TO_GESTOR_SENT)
    assert q == ProductionStatus.IN_PRODUCTION


def test_production_update_keeps_in_production():
    p, q = apply_event(
        PortalStatus.SENT_TO_FIRE, ProductionStatus.IN_PRODUCTION,
        LifecycleEvent.PRODUCTION_UPDATE,
    )
    assert p == PortalStatus.SENT_TO_FIRE
    assert q == ProductionStatus.IN_PRODUCTION


def test_production_completed_terminal():
    p, q = apply_event(
        PortalStatus.SENT_TO_FIRE, ProductionStatus.IN_PRODUCTION,
        LifecycleEvent.PRODUCTION_COMPLETED,
    )
    assert q == ProductionStatus.COMPLETED


def test_is_valid_matches_apply():
    """is_valid() must agree with apply_event() — same transition table."""
    for portal in PortalStatus:
        for prod in ProductionStatus:
            for event in LifecycleEvent:
                valid = is_valid(portal, prod, event)
                if valid:
                    apply_event(portal, prod, event)  # must not raise
                else:
                    with pytest.raises(InvalidTransitionError):
                        apply_event(portal, prod, event)


def test_transition_tables_are_consistent():
    """Every event listed in PORTAL_TRANSITIONS must have a matching production
    transition for at least one production_status. Otherwise the event is
    reachable on portal axis but never on production axis — bug magnet."""
    portal_events = {ev for (_, ev) in PORTAL_TRANSITIONS}
    production_events = {ev for (_, ev) in PRODUCTION_TRANSITIONS}
    assert portal_events <= production_events, (
        f"Events allowed on portal but not on production: {portal_events - production_events}"
    )


# ── DB-backed transition() ────────────────────────────────────────────────


def test_transition_persists_event_and_projects_state(sqlite_tmp):
    iid = _seed_parsed()
    result = transition(
        iid, LifecycleEvent.SEND_TO_FIRE_SUCCEEDED,
        source=EventSource.PORTAL,
        payload={"fire_codigo": 42},
        trace_id="trace-abc",
    )
    assert result.portal_status == PortalStatus.SENT_TO_FIRE
    assert result.production_status == ProductionStatus.NONE
    assert result.state_version == 2  # from default 1 to 2

    entry = repo.get_import(iid)
    assert entry["portal_status"] == "sent_to_fire"
    assert entry["state_version"] == 2

    events = list_events(iid)
    assert len(events) == 1
    assert events[0]["event_type"] == "send_to_fire_succeeded"
    assert events[0]["source"] == "portal"
    assert events[0]["trace_id"] == "trace-abc"
    assert events[0]["payload"] == {"fire_codigo": 42}


def test_transition_invalid_raises(sqlite_tmp):
    iid = _seed_parsed()
    transition(iid, LifecycleEvent.SEND_TO_FIRE_SUCCEEDED, source=EventSource.PORTAL)
    with pytest.raises(InvalidTransitionError):
        # Cannot import again from sent_to_fire
        transition(iid, LifecycleEvent.IMPORTED, source=EventSource.PORTAL)


def test_transition_invalid_does_not_append_event(sqlite_tmp):
    iid = _seed_parsed()
    transition(iid, LifecycleEvent.SEND_TO_FIRE_SUCCEEDED, source=EventSource.PORTAL)
    pre = list_events(iid)
    with pytest.raises(InvalidTransitionError):
        transition(iid, LifecycleEvent.IMPORTED, source=EventSource.PORTAL)
    post = list_events(iid)
    assert len(pre) == len(post), "event log must not grow on rejected transition"


def test_transition_unknown_import_id(sqlite_tmp):
    with pytest.raises(LookupError):
        transition("does-not-exist", LifecycleEvent.IMPORTED, source=EventSource.PORTAL)


def test_optimistic_concurrency_blocks_stale_writers(sqlite_tmp):
    iid = _seed_parsed()
    # Worker reads version=1
    entry = repo.get_import(iid)
    assert entry["state_version"] == 1
    # Another path bumps to 2
    transition(iid, LifecycleEvent.IMPORTED, source=EventSource.PORTAL)
    # Worker tries to write with stale expected_state_version
    with pytest.raises(StaleStateError):
        transition(
            iid, LifecycleEvent.SEND_TO_FIRE_SUCCEEDED,
            source=EventSource.PORTAL,
            expected_state_version=1,
        )


def test_replay_matches_projection_for_canonical_flow(sqlite_tmp):
    """Property check on the canonical commit→send→done flow."""
    iid = _seed_parsed()
    transition(iid, LifecycleEvent.IMPORTED, source=EventSource.PORTAL)
    transition(iid, LifecycleEvent.SEND_TO_FIRE_SUCCEEDED, source=EventSource.PORTAL)

    entry = repo.get_import(iid)
    replayed = replay_state(iid)
    assert replayed[0].value == entry["portal_status"]
    assert replayed[1].value == entry["production_status"]


def test_replay_matches_projection_random_walks(sqlite_tmp):
    """Drive the SM with random valid events and check replay == projection."""
    rng = random.Random(42)
    for _ in range(20):
        iid = _seed_parsed()
        portal = PortalStatus.PARSED
        prod = ProductionStatus.NONE
        for _ in range(rng.randint(0, 6)):
            valid_events = [
                ev for ev in LifecycleEvent
                if is_valid(portal, prod, ev)
            ]
            if not valid_events:
                break
            ev = rng.choice(valid_events)
            transition(iid, ev, source=EventSource.SYSTEM)
            portal, prod = apply_event(portal, prod, ev)

        entry = repo.get_import(iid)
        replayed = replay_state(iid)
        assert replayed[0].value == entry["portal_status"], (
            f"drift on {iid}: log replay={replayed[0]} projection={entry['portal_status']}"
        )
        assert replayed[1].value == entry["production_status"]


def test_invalid_transition_does_not_bump_state_version(sqlite_tmp):
    iid = _seed_parsed()
    transition(iid, LifecycleEvent.SEND_TO_FIRE_SUCCEEDED, source=EventSource.PORTAL)
    pre_version = repo.get_import(iid)["state_version"]
    with pytest.raises(InvalidTransitionError):
        transition(iid, LifecycleEvent.IMPORTED, source=EventSource.PORTAL)
    post_version = repo.get_import(iid)["state_version"]
    assert pre_version == post_version


def test_lifecycle_events_cascade_on_import_delete(sqlite_tmp):
    iid = _seed_parsed()
    transition(iid, LifecycleEvent.IMPORTED, source=EventSource.PORTAL)
    transition(iid, LifecycleEvent.SEND_TO_FIRE_SUCCEEDED, source=EventSource.PORTAL)
    assert len(list_events(iid)) == 2
    with db.connect() as conn:
        conn.execute("DELETE FROM imports WHERE id = ?", (iid,))
    assert list_events(iid) == []


def test_repo_insert_does_not_clobber_status(sqlite_tmp):
    """After SM moves portal_status, a re-upsert must NOT regress it."""
    iid = _seed_parsed()
    transition(iid, LifecycleEvent.SEND_TO_FIRE_SUCCEEDED, source=EventSource.PORTAL)
    # Simulate a legacy code path calling insert_import again with old status
    repo.insert_import({
        "id": iid,
        "source_filename": "pedido.pdf",
        "imported_at": datetime.now().isoformat(timespec="seconds"),
        "portal_status": "parsed",  # would regress!
        "status": "success",
    })
    entry = repo.get_import(iid)
    assert entry["portal_status"] == "sent_to_fire", (
        "insert_import upsert must not clobber SM-owned columns"
    )


def test_trace_id_falls_back_to_contextvar(sqlite_tmp):
    """If trace_id arg omitted, transition() picks up ContextVar."""
    from app.observability.trace import with_trace_id

    iid = _seed_parsed()
    with with_trace_id("ctx-trace-xyz"):
        transition(iid, LifecycleEvent.IMPORTED, source=EventSource.PORTAL)
    events = list_events(iid)
    assert events[-1]["trace_id"] == "ctx-trace-xyz"
