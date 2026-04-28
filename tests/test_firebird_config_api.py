"""Integration tests for /api/firebird/* and the new /configuracoes/* HTML routes."""
from __future__ import annotations

import os
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient

from app.persistence import db


@pytest.fixture
def isolated_app(tmp_path: Path, monkeypatch, real_auth):
    """Fresh DB + real auth flow + isolated firebird config files."""
    monkeypatch.setenv("INPUT_DIR", str(tmp_path / "input"))
    monkeypatch.setenv("OUTPUT_DIR", str(tmp_path / "output"))
    db.set_db_path(tmp_path / "app_state.db")
    db.reset_init_cache()
    db.init()

    # Redirect firebird_config + secret_store to per-test files
    from app import firebird_config
    from app.security import secret_store

    monkeypatch.setattr(firebird_config, "_CONFIG_FILE", tmp_path / "firebird.json")
    monkeypatch.setattr(secret_store, "_KEY_FILE", tmp_path / ".secret.key")

    # Make sure each test starts with no FB_* env contamination
    for k in ("FB_DATABASE", "FB_HOST", "FB_PORT", "FB_USER", "FB_CHARSET", "FB_PASSWORD"):
        monkeypatch.delenv(k, raising=False)
    yield
    db.set_db_path(None)
    db.reset_init_cache()


def _bootstrap_admin(c: TestClient, email: str = "admin@x.com", pw: str = "supersecret1") -> None:
    r = c.post("/api/auth/bootstrap", json={"email": email, "password": pw})
    assert r.status_code == 200, r.text


def _login(c: TestClient, email: str, password: str) -> bool:
    r = c.post("/api/auth/login", json={"email": email, "password": password})
    return r.status_code == 200


# ── GET /api/firebird/config ────────────────────────────────────────────


def test_get_firebird_config_requires_auth(isolated_app):
    from app.web.server import app
    c = TestClient(app)
    r = c.get("/api/firebird/config")
    assert r.status_code == 401


def test_get_firebird_config_returns_empty_when_unset(isolated_app):
    from app.web.server import app
    c = TestClient(app)
    _bootstrap_admin(c)
    r = c.get("/api/firebird/config")
    assert r.status_code == 200
    body = r.json()
    assert body["path"] == ""
    assert body["configured"] is False
    assert body["passwordSet"] is False
    # Password must NEVER be exposed
    assert "password" not in body
    assert "password_enc" not in body


def test_get_firebird_config_visible_to_operator(isolated_app):
    from app.web.server import app
    c = TestClient(app)
    _bootstrap_admin(c)
    c.post("/api/admin/users", json={
        "email": "op@x.com", "password": "operpass1", "role": "operator",
    })
    c.post("/api/auth/logout")
    assert _login(c, "op@x.com", "operpass1")
    r = c.get("/api/firebird/config")
    assert r.status_code == 200
    body = r.json()
    # Read is allowed to any logged-in user; password still hidden
    assert "password" not in body


# ── POST /api/firebird/config ───────────────────────────────────────────


def test_post_firebird_config_rejects_anonymous(isolated_app):
    from app.web.server import app
    c = TestClient(app)
    r = c.post("/api/firebird/config", json={"path": "/x.fdb"})
    assert r.status_code == 401


def test_post_firebird_config_rejects_operator(isolated_app):
    from app.web.server import app
    c = TestClient(app)
    _bootstrap_admin(c)
    c.post("/api/admin/users", json={
        "email": "op@x.com", "password": "operpass1", "role": "operator",
    })
    c.post("/api/auth/logout")
    assert _login(c, "op@x.com", "operpass1")
    r = c.post("/api/firebird/config", json={"path": "/x.fdb"})
    assert r.status_code == 403


def test_post_firebird_config_persists_and_applies_to_env(isolated_app):
    from app import firebird_config
    from app.web.server import app
    c = TestClient(app)
    _bootstrap_admin(c)
    r = c.post("/api/firebird/config", json={
        "path": "/data/empresa.fdb", "host": "10.0.0.1", "port": "3050",
        "user": "SYSDBA", "charset": "WIN1252", "password": "masterkey",
    })
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["configured"] is True
    assert body["passwordSet"] is True
    # apply_to_env was called: env reflects the saved values
    assert os.environ["FB_DATABASE"] == "/data/empresa.fdb"
    assert os.environ["FB_PASSWORD"] == "masterkey"
    # Password is encrypted on disk
    enc = firebird_config.load()["password_enc"]
    assert enc and "masterkey" not in enc
    # Decryption round-trips
    assert firebird_config.get_password() == "masterkey"


def test_post_firebird_config_omitting_password_keeps_existing(isolated_app):
    from app import firebird_config
    from app.web.server import app
    c = TestClient(app)
    _bootstrap_admin(c)
    c.post("/api/firebird/config", json={"path": "/a.fdb", "password": "first"})
    # Update path only — password absent in body should keep "first"
    c.post("/api/firebird/config", json={"path": "/b.fdb"})
    assert firebird_config.get_password() == "first"
    assert firebird_config.load()["path"] == "/b.fdb"


# ── POST /api/firebird/test ─────────────────────────────────────────────


def test_test_connection_requires_admin(isolated_app):
    from app.web.server import app
    c = TestClient(app)
    _bootstrap_admin(c)
    c.post("/api/admin/users", json={
        "email": "op@x.com", "password": "operpass1", "role": "operator",
    })
    c.post("/api/auth/logout")
    assert _login(c, "op@x.com", "operpass1")
    r = c.post("/api/firebird/test", json={"path": "/x.fdb"})
    assert r.status_code == 403


def test_test_connection_missing_path_returns_400(isolated_app):
    from app.web.server import app
    c = TestClient(app)
    _bootstrap_admin(c)
    r = c.post("/api/firebird/test", json={})
    assert r.status_code == 400
    body = r.json()
    assert body["ok"] is False
    assert "path" in body["error"].lower() or "caminho" in body["error"].lower()
    assert "traceId" in body


def test_test_connection_success(isolated_app, monkeypatch):
    """When the driver succeeds, endpoint reports ok=True."""
    from app.erp import connection as conn_mod
    from app.web.server import app

    # Mock the firebird driver's connect() to return a fake connection
    fake_cur = MagicMock()
    fake_cur.fetchone.return_value = (1,)
    fake_conn = MagicMock()
    fake_conn.cursor.return_value = fake_cur

    fake_module = MagicMock()
    fake_module.connect.return_value = fake_conn
    monkeypatch.setitem(__import__("sys").modules, "firebird.driver", fake_module)
    # Connection module imports lazily inside connect(); ensure our stub is what gets imported
    import sys
    sys.modules["firebird"] = MagicMock(driver=fake_module)
    sys.modules["firebird.driver"] = fake_module

    c = TestClient(app)
    _bootstrap_admin(c)
    r = c.post("/api/firebird/test", json={
        "path": "/data/empresa.fdb", "user": "SYSDBA",
        "charset": "WIN1252", "password": "masterkey",
    })
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["ok"] is True
    assert "traceId" in body


def test_test_connection_driver_error_returns_400_with_trace(isolated_app, monkeypatch):
    """Driver errors are wrapped into FirebirdConnectionError → 400 with trace_id."""
    import sys
    from app.web.server import app

    def boom(**_kwargs):
        raise RuntimeError("io error: file not found")

    fake_module = MagicMock()
    fake_module.connect.side_effect = boom
    sys.modules["firebird"] = MagicMock(driver=fake_module)
    sys.modules["firebird.driver"] = fake_module

    c = TestClient(app)
    _bootstrap_admin(c)
    r = c.post("/api/firebird/test", json={"path": "/nope.fdb", "password": "x"})
    assert r.status_code == 400
    body = r.json()
    assert body["ok"] is False
    assert "traceId" in body
    assert body["error"]


# ── HTML routes + redirect ──────────────────────────────────────────────


def test_admin_usuarios_redirects_301(isolated_app):
    from app.web.server import app
    c = TestClient(app, follow_redirects=False)
    r = c.get("/admin/usuarios")
    assert r.status_code == 301
    assert r.headers["location"] == "/configuracoes/usuarios"


def test_configuracoes_usuarios_serves_html(isolated_app):
    from app.web.server import app
    c = TestClient(app)
    r = c.get("/configuracoes/usuarios")
    assert r.status_code == 200
    assert "text/html" in r.headers["content-type"]


def test_configuracoes_banco_serves_html(isolated_app):
    from app.web.server import app
    c = TestClient(app)
    r = c.get("/configuracoes/banco")
    assert r.status_code == 200
    assert "text/html" in r.headers["content-type"]


def test_configuracoes_diretorios_serves_html(isolated_app):
    from app.web.server import app
    c = TestClient(app)
    r = c.get("/configuracoes/diretorios")
    assert r.status_code == 200
    assert "text/html" in r.headers["content-type"]


def test_api_config_includes_firebird_configured_flag(isolated_app):
    from app.web.server import app
    c = TestClient(app)
    r = c.get("/api/config")
    assert r.status_code == 200
    assert "firebirdConfigured" in r.json()
