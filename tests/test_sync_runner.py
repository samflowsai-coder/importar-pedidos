from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

from app.persistence import db, environments_repo, router
from app.persistence.context import active_env
from app.sync import runner, sync_state_repo
from app.sync.models import (
    ComponentRow,
    ProductRow,
    RunStatus,
    Trigger,
)


@pytest.fixture
def env(tmp_path: Path):
    db.set_db_path(tmp_path)
    e = environments_repo.create(
        slug="acme", name="ACME",
        watch_dir=str(tmp_path / "in"), output_dir=str(tmp_path / "out"),
        fb_path="/tmp/x.fdb", fb_password="x",
    )
    Path(e["watch_dir"]).mkdir(exist_ok=True)
    Path(e["output_dir"]).mkdir(exist_ok=True)
    environments_repo.set_flowpcp_config(
        env_id=e["id"], enabled=True,
        base_url="https://flowpcp.test", tenant_id="t-1",
        api_key="pp_live_x",
    )
    yield environments_repo.get(e["id"])
    db.set_db_path(None)
    router.reset_init_cache()


def _product(seq, descr="X", inativo=False, is_kit=False):
    return ProductRow(
        seq=seq, codprod_altern=None, descricao=descr,
        unidade="un", codigo_ean13=None, inativo=inativo, is_kit=is_kit,
    )


def test_runner_happy_path(env):
    products = [_product(1, "Tenis")]

    class FakeResp:
        sync_id = ""
        applied = {"produtos": 1, "componentes": 0, "tombstones": 0}
        skipped = 0
        errors = []

    with patch("app.sync.runner.read_products_snapshot", return_value=products), \
         patch("app.sync.runner.read_components_snapshot", return_value=[]), \
         patch("app.sync.runner.FlowPCPClient") as client_mock:
        client_mock.return_value.sync_products.return_value = FakeResp()
        result = runner.run(env=env, trigger=Trigger.MANUAL)

    assert result.status == RunStatus.APPLIED
    assert result.delta_count_produtos == 1
    assert result.applied_count == 1

    # State should be committed
    with active_env(env["id"], env["slug"]):
        state = sync_state_repo.load_product_state()
    assert 1 in state


def test_runner_skips_when_disabled(env):
    environments_repo.set_flowpcp_config(
        env_id=env["id"], enabled=False,
        base_url=env["flowpcp_base_url"], tenant_id=env["flowpcp_tenant_id"],
        api_key=None,
    )
    fresh = environments_repo.get(env["id"])
    result = runner.run(env=fresh, trigger=Trigger.MANUAL)
    assert result.status == RunStatus.FAILED
    assert any(e.reason == "flowpcp_disabled" for e in result.errors)


def test_runner_failure_does_not_commit_state(env):
    products = [_product(1, "Tenis")]
    from app.integrations.flowpcp.client import FlowPCPClientError

    with patch("app.sync.runner.read_products_snapshot", return_value=products), \
         patch("app.sync.runner.read_components_snapshot", return_value=[]), \
         patch("app.sync.runner.FlowPCPClient") as client_mock:
        client_mock.return_value.sync_products.side_effect = FlowPCPClientError(
            "boom", status_code=503
        )
        result = runner.run(env=env, trigger=Trigger.MANUAL)

    assert result.status == RunStatus.FAILED
    with active_env(env["id"], env["slug"]):
        assert sync_state_repo.load_product_state() == {}


def test_runner_empty_delta_returns_applied(env):
    with patch("app.sync.runner.read_products_snapshot", return_value=[]), \
         patch("app.sync.runner.read_components_snapshot", return_value=[]):
        result = runner.run(env=env, trigger=Trigger.MANUAL)
    assert result.status == RunStatus.APPLIED
    assert result.delta_count_produtos == 0


def test_runner_circuit_open_skips(env):
    for _ in range(5):
        environments_repo.mark_flowpcp_failure(env_id=env["id"], threshold=5)
    fresh = environments_repo.get(env["id"])
    assert fresh["flowpcp_circuit_open"] == 1

    result = runner.run(env=fresh, trigger=Trigger.SCHEDULER)
    assert result.status == RunStatus.FAILED
    assert any(e.reason == "circuit_open" for e in result.errors)


def test_runner_partial_when_server_returns_errors(env):
    products = [_product(1, "Tenis")]
    components: list[ComponentRow] = []

    class FakeErr:
        codigo = "1"
        reason = "componente_filho_inexistente"

    class FakeResp:
        sync_id = ""
        applied = {"produtos": 0, "componentes": 0, "tombstones": 0}
        skipped = 1
        errors = [FakeErr()]

    with patch("app.sync.runner.read_products_snapshot", return_value=products), \
         patch("app.sync.runner.read_components_snapshot", return_value=components), \
         patch("app.sync.runner.FlowPCPClient") as client_mock:
        client_mock.return_value.sync_products.return_value = FakeResp()
        result = runner.run(env=env, trigger=Trigger.MANUAL)

    assert result.status == RunStatus.PARTIAL
    assert len(result.errors) == 1
    assert result.errors[0].reason == "componente_filho_inexistente"

    # State of seq=1 NOT committed (it had an error)
    with active_env(env["id"], env["slug"]):
        state = sync_state_repo.load_product_state()
    assert 1 not in state


def test_runner_fire_read_failure_marks_circuit_failure(env):
    with patch("app.sync.runner.read_products_snapshot", side_effect=RuntimeError("FB down")):
        result = runner.run(env=env, trigger=Trigger.SCHEDULER)
    assert result.status == RunStatus.FAILED
    assert any(e.reason.startswith("fire_read_failed") for e in result.errors)
    fresh = environments_repo.get(env["id"])
    assert fresh["flowpcp_consecutive_failures"] >= 1
