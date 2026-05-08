"""SQLite state for product sync (per-environment)."""
from __future__ import annotations

from pathlib import Path

import pytest

from app.persistence import db, environments_repo, router
from app.persistence.context import active_env
from app.sync import sync_state_repo
from app.sync.models import RunResult, RunStatus, SyncError, Trigger


@pytest.fixture
def tmp_data(tmp_path: Path):
    db.set_db_path(tmp_path)
    yield tmp_path
    db.set_db_path(None)
    router.reset_init_cache()


@pytest.fixture
def env_active(tmp_data):
    env = environments_repo.create(
        slug="acme", name="ACME",
        watch_dir=str(tmp_data / "in"), output_dir=str(tmp_data / "out"),
        fb_path="/tmp/x.fdb", fb_password="x",
    )
    (tmp_data / "in").mkdir(exist_ok=True)
    (tmp_data / "out").mkdir(exist_ok=True)
    with active_env(env["id"], env["slug"]):
        yield env


def test_per_env_schema_has_sync_tables(tmp_data):
    with router.env_connect("test") as conn:
        names = {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()}
        cols_state = {r[1] for r in conn.execute(
            "PRAGMA table_info(product_sync_state)").fetchall()}
        cols_components = {r[1] for r in conn.execute(
            "PRAGMA table_info(component_sync_state)").fetchall()}
        cols_runs = {r[1] for r in conn.execute(
            "PRAGMA table_info(product_sync_runs)").fetchall()}

    assert "product_sync_state" in names
    assert "component_sync_state" in names
    assert "product_sync_runs" in names

    assert {"seq", "content_hash", "last_synced_at"}.issubset(cols_state)
    assert {"codigo", "content_hash", "last_synced_at"}.issubset(cols_components)
    assert {"id", "environment_id", "sync_id", "trigger", "started_at", "finished_at",
            "status", "delta_count_produtos", "delta_count_componentes",
            "delta_count_tombstones", "applied_count", "errors_json",
            "trace_id"}.issubset(cols_runs)


def test_load_returns_empty_dict_initially(env_active):
    assert sync_state_repo.load_product_state() == {}
    assert sync_state_repo.load_component_state() == {}


def test_commit_states_inserts_new(env_active):
    sync_state_repo.commit_states(
        product_upserts={1: "h1", 2: "h2"},
        product_tombstones=[],
        component_upserts={10: "ch1"},
        component_tombstones=[],
    )
    assert sync_state_repo.load_product_state() == {1: "h1", 2: "h2"}
    assert sync_state_repo.load_component_state() == {10: "ch1"}


def test_commit_states_updates_existing_and_removes_tombstones(env_active):
    sync_state_repo.commit_states(
        product_upserts={1: "h1", 2: "h2"},
        product_tombstones=[],
        component_upserts={},
        component_tombstones=[],
    )
    sync_state_repo.commit_states(
        product_upserts={2: "h2_new"},
        product_tombstones=[1],
        component_upserts={},
        component_tombstones=[],
    )
    assert sync_state_repo.load_product_state() == {2: "h2_new"}


def test_record_run_lifecycle(env_active):
    sync_state_repo.record_run_start(
        sync_id="01HX",
        trigger=Trigger.MANUAL,
        trace_id="t-123",
    )
    runs = sync_state_repo.list_runs(limit=10)
    assert len(runs) == 1
    assert runs[0]["sync_id"] == "01HX"
    assert runs[0]["status"] == "running"

    sync_state_repo.record_run_finish(
        sync_id="01HX",
        result=RunResult(
            sync_id="01HX",
            status=RunStatus.APPLIED,
            delta_count_produtos=2,
            delta_count_componentes=1,
            delta_count_tombstones=0,
            applied_count=3,
            errors=[],
        ),
    )
    runs = sync_state_repo.list_runs(limit=10)
    assert runs[0]["status"] == "applied"
    assert runs[0]["applied_count"] == 3


def test_record_run_finish_with_errors_persists_json(env_active):
    sync_state_repo.record_run_start(sync_id="01HY", trigger=Trigger.SCHEDULER, trace_id=None)
    sync_state_repo.record_run_finish(
        sync_id="01HY",
        result=RunResult(
            sync_id="01HY",
            status=RunStatus.PARTIAL,
            errors=[SyncError(codigo="42", reason="componente_filho_inexistente")],
        ),
    )
    runs = sync_state_repo.list_runs(limit=10)
    import json as _json
    errs = _json.loads(runs[0]["errors_json"])
    assert errs == [{"codigo": "42", "reason": "componente_filho_inexistente"}]


def test_record_run_persists_environment_id(env_active):
    sync_state_repo.record_run_start(sync_id="01HZ", trigger=Trigger.MANUAL, trace_id=None)
    runs = sync_state_repo.list_runs(limit=10)
    assert runs[0]["environment_id"] == env_active["id"]


def test_consecutive_failure_count(env_active):
    # No runs yet
    assert sync_state_repo.consecutive_failure_count() == 0

    # Add 3 failed runs
    for i in range(3):
        sync_state_repo.record_run_start(sync_id=f"f{i}", trigger=Trigger.SCHEDULER, trace_id=None)
        sync_state_repo.record_run_finish(
            sync_id=f"f{i}",
            result=RunResult(sync_id=f"f{i}", status=RunStatus.FAILED),
        )
    assert sync_state_repo.consecutive_failure_count() == 3

    # An applied run resets the streak
    sync_state_repo.record_run_start(sync_id="ok", trigger=Trigger.SCHEDULER, trace_id=None)
    sync_state_repo.record_run_finish(
        sync_id="ok",
        result=RunResult(sync_id="ok", status=RunStatus.APPLIED),
    )
    assert sync_state_repo.consecutive_failure_count() == 0
