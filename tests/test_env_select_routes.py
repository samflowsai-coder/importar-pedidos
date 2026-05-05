"""Rotas /api/env/list, /api/env/select e middleware de ambiente."""
from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.persistence import db, environments_repo, router


@pytest.fixture
def app_setup(tmp_path: Path):
    """Sobe APP_DATA_DIR isolado, cria 2 ambientes ativos."""
    import os
    os.environ["APP_DATA_DIR"] = str(tmp_path)
    db.set_db_path(tmp_path / "app_state.db")
    db.reset_init_cache()
    db.init()
    env_mm = environments_repo.create(
        slug="mm", name="MM Calçados",
        watch_dir=str(tmp_path / "mm-in"),
        output_dir=str(tmp_path / "mm-out"),
        fb_path=str(tmp_path / "mm.fdb"),
    )
    env_nm = environments_repo.create(
        slug="nasmar", name="Nasmar",
        watch_dir=str(tmp_path / "nm-in"),
        output_dir=str(tmp_path / "nm-out"),
        fb_path=str(tmp_path / "nm.fdb"),
    )
    yield env_mm, env_nm
    db.set_db_path(None)
    db.reset_init_cache()
    os.environ.pop("APP_DATA_DIR", None)


def _client():
    from app.web.server import app
    return TestClient(app)


def test_list_envs_returns_active_only(app_setup):
    env_mm, env_nm = app_setup
    environments_repo.soft_delete(env_nm["id"])
    c = _client()
    r = c.get("/api/env/list")
    assert r.status_code == 200
    body = r.json()
    assert {e["slug"] for e in body} == {"mm"}
    assert all("fb_password" not in e for e in body)


def test_select_env_sets_cookie(app_setup):
    env_mm, _ = app_setup
    c = _client()
    r = c.post("/api/env/select", json={"environment_id": env_mm["id"]})
    assert r.status_code == 200
    assert r.json()["environment"]["slug"] == "mm"
    assert "portal_env" in r.cookies


def test_select_env_404_for_unknown(app_setup):
    c = _client()
    r = c.post("/api/env/select", json={"environment_id": "no-such-id"})
    assert r.status_code == 404


def test_select_env_404_for_inactive(app_setup):
    env_mm, _ = app_setup
    environments_repo.soft_delete(env_mm["id"])
    c = _client()
    r = c.post("/api/env/select", json={"environment_id": env_mm["id"]})
    assert r.status_code == 404


def test_auth_me_returns_environment_when_cookie_present(app_setup):
    env_mm, _ = app_setup
    c = _client()
    c.cookies.set("portal_env", env_mm["id"])
    r = c.get("/api/auth/me")
    assert r.status_code == 200
    body = r.json()
    assert body["environment"]["slug"] == "mm"


def test_auth_me_environment_null_when_cookie_invalid(app_setup):
    c = _client()
    c.cookies.set("portal_env", "fake-id")
    r = c.get("/api/auth/me")
    assert r.status_code == 200
    assert r.json()["environment"] is None


def test_auth_me_environment_null_when_inactive(app_setup):
    env_mm, _ = app_setup
    environments_repo.soft_delete(env_mm["id"])
    c = _client()
    c.cookies.set("portal_env", env_mm["id"])
    r = c.get("/api/auth/me")
    assert r.status_code == 200
    assert r.json()["environment"] is None


def test_root_redirects_to_select_env_without_cookie(app_setup, monkeypatch):
    """Sem cookie portal_env e sem TEST_AUTH_BYPASS, root → /selecionar-ambiente."""
    monkeypatch.delenv("TEST_AUTH_BYPASS", raising=False)
    c = _client()
    c.cookies.set("portal_session", "fake-session")
    r = c.get("/", follow_redirects=False)
    assert r.status_code in (302, 307)
    assert "/selecionar-ambiente" in r.headers["location"]


def test_root_redirects_to_login_without_session(app_setup, monkeypatch):
    monkeypatch.delenv("TEST_AUTH_BYPASS", raising=False)
    c = _client()
    r = c.get("/", follow_redirects=False)
    assert r.status_code in (302, 307)
    assert "/login" in r.headers["location"]
