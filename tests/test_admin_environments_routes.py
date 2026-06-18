"""Rotas /api/admin/environments (CRUD)."""
from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.persistence import db, environments_repo, router


@pytest.fixture
def setup(tmp_path: Path):
    import os
    os.environ["APP_DATA_DIR"] = str(tmp_path)
    db.set_db_path(tmp_path / "app_state.db")
    db.reset_init_cache()
    db.init()
    yield tmp_path
    db.set_db_path(None)
    db.reset_init_cache()
    os.environ.pop("APP_DATA_DIR", None)


def _client():
    from app.web.server import app
    return TestClient(app)


def test_list_empty(setup):
    r = _client().get("/api/admin/environments")
    assert r.status_code == 200
    assert r.json() == []


def test_create_returns_public_view(setup):
    payload = {
        "slug": "mm", "name": "MM",
        "watch_dir": str(setup / "in"),
        "output_dir": str(setup / "out"),
        "fb_path": str(setup / "x.fdb"),
        "fb_password": "secret",
    }
    r = _client().post("/api/admin/environments", json=payload)
    assert r.status_code == 201
    body = r.json()
    assert body["slug"] == "mm"
    assert body["name"] == "MM"
    assert body["is_active"] == 1
    # senha jamais retorna
    assert "fb_password" not in body
    assert "fb_password_enc" not in body


def test_create_rejects_duplicate_slug(setup):
    payload = {
        "slug": "mm", "name": "MM",
        "watch_dir": str(setup), "output_dir": str(setup),
        "fb_path": "/x.fdb",
    }
    c = _client()
    c.post("/api/admin/environments", json=payload)
    r = c.post("/api/admin/environments", json=payload)
    assert r.status_code == 409


def test_create_rejects_bad_slug(setup):
    payload = {
        "slug": "MM Calçados", "name": "MM",
        "watch_dir": str(setup), "output_dir": str(setup),
        "fb_path": "/x.fdb",
    }
    r = _client().post("/api/admin/environments", json=payload)
    assert r.status_code == 400


def test_get_returns_404_for_missing(setup):
    r = _client().get("/api/admin/environments/nope")
    assert r.status_code == 404


def test_patch_keeps_slug_immutable(setup):
    c = _client()
    payload = {
        "slug": "mm", "name": "MM",
        "watch_dir": str(setup), "output_dir": str(setup),
        "fb_path": "/x.fdb",
    }
    created = c.post("/api/admin/environments", json=payload).json()
    env_id = created["id"]
    # PATCH não declara slug — UpdateEnvRequest omite o campo
    r = c.patch(f"/api/admin/environments/{env_id}", json={"name": "MM Renomeado"})
    assert r.status_code == 200
    body = r.json()
    assert body["name"] == "MM Renomeado"
    assert body["slug"] == "mm"


def test_patch_password_modes(setup):
    c = _client()
    payload = {
        "slug": "mm", "name": "MM",
        "watch_dir": str(setup), "output_dir": str(setup),
        "fb_path": "/x.fdb",
        "fb_password": "orig",
    }
    created = c.post("/api/admin/environments", json=payload).json()
    env_id = created["id"]
    # 1) sem fb_password → mantém
    c.patch(f"/api/admin/environments/{env_id}", json={"name": "MM2"})
    assert environments_repo.get_password(env_id) == "orig"
    # 2) "" → limpa
    c.patch(f"/api/admin/environments/{env_id}", json={"fb_password": ""})
    assert environments_repo.get_password(env_id) is None
    # 3) valor → substitui
    c.patch(f"/api/admin/environments/{env_id}", json={"fb_password": "novasenha"})
    assert environments_repo.get_password(env_id) == "novasenha"


def test_delete_soft_removes_from_list(setup):
    c = _client()
    payload = {
        "slug": "mm", "name": "MM",
        "watch_dir": str(setup), "output_dir": str(setup),
        "fb_path": "/x.fdb",
    }
    env_id = c.post("/api/admin/environments", json=payload).json()["id"]
    r = c.delete(f"/api/admin/environments/{env_id}")
    assert r.status_code == 204
    after = c.get("/api/admin/environments").json()
    # list_all retorna inactive também, mas com is_active=0
    rec = next(e for e in after if e["id"] == env_id)
    assert rec["is_active"] == 0


def test_test_endpoint_validates_paths(setup):
    c = _client()
    payload = {
        "slug": "mm", "name": "MM",
        "watch_dir": str(setup / "naoexiste"),
        "output_dir": str(setup / "naoexiste-tb"),
        "fb_path": "/inexistente.fdb",
    }
    env_id = c.post("/api/admin/environments", json=payload).json()["id"]
    r = c.post(f"/api/admin/environments/{env_id}/test")
    assert r.status_code == 200
    body = r.json()
    assert body["watch_dir_ok"] is False
    assert body["output_dir_ok"] is False
    assert body["firebird_ok"] is False
    assert body["firebird_error"]


def test_test_endpoint_validates_existing_paths(setup):
    in_dir = setup / "in"; out_dir = setup / "out"
    in_dir.mkdir(); out_dir.mkdir()
    c = _client()
    payload = {
        "slug": "mm", "name": "MM",
        "watch_dir": str(in_dir), "output_dir": str(out_dir),
        "fb_path": "/inexistente.fdb",  # FB falha mas pastas OK
    }
    env_id = c.post("/api/admin/environments", json=payload).json()["id"]
    r = c.post(f"/api/admin/environments/{env_id}/test")
    body = r.json()
    assert body["watch_dir_ok"] is True
    assert body["output_dir_ok"] is True
    # FB ainda falha porque o .fdb não existe
    assert body["firebird_ok"] is False
