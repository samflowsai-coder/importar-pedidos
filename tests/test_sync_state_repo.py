"""SQLite state for product sync (per-environment)."""
from __future__ import annotations

from pathlib import Path

import pytest

from app.persistence import db, router


@pytest.fixture
def tmp_data(tmp_path: Path):
    db.set_db_path(tmp_path)
    yield tmp_path
    db.set_db_path(None)
    router.reset_init_cache()


def test_per_env_schema_has_sync_tables(tmp_data):
    with router.env_connect("test") as conn:
        names = {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()}
    assert "product_sync_state" in names
    assert "component_sync_state" in names
    assert "product_sync_runs" in names

    with router.env_connect("test") as conn:
        cols_state = {r[1] for r in conn.execute(
            "PRAGMA table_info(product_sync_state)").fetchall()}
        cols_runs = {r[1] for r in conn.execute(
            "PRAGMA table_info(product_sync_runs)").fetchall()}
    assert {"seq", "content_hash", "last_synced_at"}.issubset(cols_state)
    assert {"id", "sync_id", "trigger", "started_at", "finished_at",
            "status", "delta_count_produtos", "delta_count_componentes",
            "delta_count_tombstones", "applied_count", "errors_json",
            "trace_id"}.issubset(cols_runs)
