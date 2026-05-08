"""Tests for admin product-sync routes.

GET  /admin/produtos/sync/{slug}               — env config snapshot + last 50 runs
POST /admin/produtos/sync-now/{slug}           — trigger one sync inline (manual)
POST /admin/produtos/sync/{slug}/reset-circuit — reset circuit breaker

All require admin (tested via real_auth; TEST_AUTH_BYPASS does NOT cover role
checks, so we use real_auth here to validate 401/403 for non-admins).
"""
from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from app.persistence import db, environments_repo, users_repo
from app.sync.models import RunResult, RunStatus


@pytest.fixture
def isolated_app(tmp_path: Path, monkeypatch, real_auth):
    """Fresh DB + real auth (no bypass)."""
    monkeypatch.setenv("INPUT_DIR", str(tmp_path / "input"))
    monkeypatch.setenv("OUTPUT_DIR", str(tmp_path / "output"))
    db.set_db_path(tmp_path / "app_state.db")
    db.reset_init_cache()
    db.init()
    yield
    db.set_db_path(None)
    db.reset_init_cache()


def _client() -> TestClient:
    from app.web.server import app
    return TestClient(app)


def _login_admin(client: TestClient) -> None:
    """Create an admin user and log in."""
    users_repo.create_user(email="admin@test", password="adminpass1", role="admin")
    r = client.post("/api/auth/login", json={"email": "admin@test", "password": "adminpass1"})
    assert r.status_code == 200, r.text


def _create_env_with_flowpcp() -> dict:
    e = environments_repo.create(
        slug="acme", name="ACME", watch_dir="/tmp/in", output_dir="/tmp/out",
        fb_path="/tmp/x.fdb", fb_password="x",
    )
    environments_repo.set_flowpcp_config(
        env_id=e["id"], enabled=True,
        base_url="https://flowpcp.test", tenant_id="t-1", api_key="pp_live_x",
    )
    return environments_repo.get(e["id"])


def test_get_runs_empty(isolated_app):
    c = _client()
    _login_admin(c)
    env = _create_env_with_flowpcp()
    r = c.get(f"/admin/produtos/sync/{env['slug']}")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["runs"] == []
    assert body["env"]["slug"] == "acme"
    assert body["env"]["flowpcp_enabled"] is True
    assert body["env"]["flowpcp_base_url"] == "https://flowpcp.test"


def test_post_sync_now_triggers_runner(isolated_app):
    c = _client()
    _login_admin(c)
    env = _create_env_with_flowpcp()

    with patch("app.web.routes_produtos_sync.runner.run") as run_mock:
        run_mock.return_value = RunResult(
            sync_id="01HX", status=RunStatus.APPLIED,
            delta_count_produtos=2, applied_count=2,
        )
        r = c.post(f"/admin/produtos/sync-now/{env['slug']}")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] == "applied"
    assert body["delta_count_produtos"] == 2
    run_mock.assert_called_once()


def test_post_sync_now_404_unknown_slug(isolated_app):
    c = _client()
    _login_admin(c)
    r = c.post("/admin/produtos/sync-now/missing")
    assert r.status_code == 404


def test_post_reset_circuit(isolated_app):
    c = _client()
    _login_admin(c)
    env = _create_env_with_flowpcp()
    for _ in range(5):
        environments_repo.mark_flowpcp_failure(env_id=env["id"], threshold=5)
    assert environments_repo.get(env["id"])["flowpcp_circuit_open"] == 1

    r = c.post(f"/admin/produtos/sync/{env['slug']}/reset-circuit")
    assert r.status_code == 200
    assert environments_repo.get(env["id"])["flowpcp_circuit_open"] == 0


def test_routes_require_admin(isolated_app):
    """A non-admin user should get 401 or 403 on all three routes."""
    c = _client()
    # Create a non-admin user and log in
    users_repo.create_user(email="op@test", password="operpass1", role="operator")
    login_r = c.post("/api/auth/login", json={"email": "op@test", "password": "operpass1"})
    assert login_r.status_code == 200, login_r.text

    # Create env (no login needed for repo call)
    env = _create_env_with_flowpcp()

    r = c.get(f"/admin/produtos/sync/{env['slug']}")
    assert r.status_code in (401, 403)

    r = c.post(f"/admin/produtos/sync-now/{env['slug']}")
    assert r.status_code in (401, 403)

    r = c.post(f"/admin/produtos/sync/{env['slug']}/reset-circuit")
    assert r.status_code in (401, 403)
