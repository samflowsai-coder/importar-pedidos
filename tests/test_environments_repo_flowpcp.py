"""FlowPCP-specific extensions to environments_repo."""
from __future__ import annotations

from pathlib import Path

import pytest

from app.persistence import db, environments_repo, router


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


def _make_env(slug="mm"):
    return environments_repo.create(
        slug=slug, name="MM", watch_dir="/tmp/in", output_dir="/tmp/out",
        fb_path="/tmp/x.fdb", fb_password="masterkey",
    )


def test_set_flowpcp_config_persists_encrypted(tmp_data):
    env = _make_env()
    environments_repo.set_flowpcp_config(
        env_id=env["id"],
        enabled=True,
        base_url="https://flowpcp.test",
        tenant_id="00000000-0000-0000-0000-000000000001",
        api_key="pp_live_secret",
    )
    fresh = environments_repo.get(env["id"])
    assert fresh["flowpcp_enabled"] == 1
    assert fresh["flowpcp_base_url"] == "https://flowpcp.test"
    assert fresh["flowpcp_tenant_id"] == "00000000-0000-0000-0000-000000000001"
    # public view must NOT include the encrypted blob
    assert "flowpcp_api_key_enc" not in fresh

    secret = environments_repo.get_flowpcp_secret(env["id"])
    assert secret == "pp_live_secret"


def test_set_flowpcp_config_with_api_key_none_keeps_existing(tmp_data):
    env = _make_env()
    environments_repo.set_flowpcp_config(
        env_id=env["id"], enabled=True,
        base_url="https://flowpcp.test", tenant_id="t-1", api_key="pp_live_first",
    )
    # Now update without api_key — should keep the original
    environments_repo.set_flowpcp_config(
        env_id=env["id"], enabled=True,
        base_url="https://flowpcp.test/v2", tenant_id="t-1", api_key=None,
    )
    assert environments_repo.get_flowpcp_secret(env["id"]) == "pp_live_first"
    fresh = environments_repo.get(env["id"])
    assert fresh["flowpcp_base_url"] == "https://flowpcp.test/v2"


def test_to_flowpcp_config_returns_decrypted(tmp_data):
    env = _make_env()
    environments_repo.set_flowpcp_config(
        env_id=env["id"], enabled=True,
        base_url="https://flowpcp.test",
        tenant_id="t-1", api_key="pp_live_x",
    )
    fresh = environments_repo.get(env["id"])
    cfg = environments_repo.to_flowpcp_config(fresh)
    assert cfg == {
        "enabled": True,
        "base_url": "https://flowpcp.test",
        "tenant_id": "t-1",
        "api_key": "pp_live_x",
    }


def test_to_flowpcp_config_disabled_returns_enabled_false(tmp_data):
    env = _make_env()
    cfg = environments_repo.to_flowpcp_config(env)
    assert cfg["enabled"] is False


def test_circuit_open_close(tmp_data):
    env = _make_env()
    # Two failures below threshold
    environments_repo.mark_flowpcp_failure(env_id=env["id"], threshold=3)
    environments_repo.mark_flowpcp_failure(env_id=env["id"], threshold=3)
    fresh = environments_repo.get(env["id"])
    assert fresh["flowpcp_circuit_open"] == 0

    # Third failure opens the circuit
    environments_repo.mark_flowpcp_failure(env_id=env["id"], threshold=3)
    fresh = environments_repo.get(env["id"])
    assert fresh["flowpcp_circuit_open"] == 1
    assert fresh["flowpcp_consecutive_failures"] == 3

    # Success resets
    environments_repo.mark_flowpcp_success(env_id=env["id"])
    fresh = environments_repo.get(env["id"])
    assert fresh["flowpcp_circuit_open"] == 0
    assert fresh["flowpcp_consecutive_failures"] == 0


def test_reset_circuit_explicit(tmp_data):
    env = _make_env()
    for _ in range(5):
        environments_repo.mark_flowpcp_failure(env_id=env["id"], threshold=5)
    assert environments_repo.get(env["id"])["flowpcp_circuit_open"] == 1
    environments_repo.reset_flowpcp_circuit(env["id"])
    fresh = environments_repo.get(env["id"])
    assert fresh["flowpcp_circuit_open"] == 0
    assert fresh["flowpcp_consecutive_failures"] == 0
