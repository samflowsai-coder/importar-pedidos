"""FlowPCP-specific extensions to environments_repo."""
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


def test_environments_table_has_flowpcp_columns(tmp_data):
    with router.shared_connect() as conn:
        cols = {r[1] for r in conn.execute("PRAGMA table_info(environments)").fetchall()}
    expected = {
        "flowpcp_enabled",
        "flowpcp_base_url",
        "flowpcp_tenant_id",
        "flowpcp_api_key_enc",
        "flowpcp_circuit_open",
        "flowpcp_last_failure_at",
        "flowpcp_consecutive_failures",
    }
    missing = expected - cols
    assert not missing, f"Missing FlowPCP columns: {missing}"
