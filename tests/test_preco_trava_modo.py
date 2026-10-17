"""Interruptor da trava de preço por ambiente (desligado | observando | ligado)."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.persistence import db, environments_repo, router


@pytest.fixture
def fresh_shared(tmp_path, monkeypatch):
    monkeypatch.setenv("APP_DATA_DIR", str(tmp_path))
    router.reset_init_cache()
    with router.shared_connect():
        pass
    yield tmp_path


@pytest.fixture
def env(fresh_shared):
    return environments_repo.create(
        slug="mm", name="MM", watch_dir="/tmp/in", output_dir="/tmp/out", fb_path="/tmp/x.fdb"
    )


def test_ambiente_novo_nasce_desligado(env):
    assert env["preco_trava_modo"] == "desligado"
    assert env["preco_fonte_slug"] is None


def test_set_modo_invalido_recusa_sem_mudar(env):
    with pytest.raises(ValueError):
        environments_repo.set_preco_trava_modo(env["id"], "talvez", por="x")
    assert environments_repo.get_by_slug("mm")["preco_trava_modo"] == "desligado"


def test_set_modo_ligado_aparece_no_get_by_slug(env):
    environments_repo.set_preco_trava_modo(env["id"], "ligado", por="admin@x.com")
    assert environments_repo.get_by_slug("mm")["preco_trava_modo"] == "ligado"
    environments_repo.set_preco_trava_modo(env["id"], "observando", por="admin@x.com")
    assert environments_repo.get_by_slug("mm")["preco_trava_modo"] == "observando"


def test_banco_antigo_migra_coluna(env):
    # Simula banco anterior à feature: tira as colunas novas e força nova migração.
    with router.shared_connect() as conn:
        conn.execute("ALTER TABLE environments DROP COLUMN preco_trava_modo")
        conn.execute("ALTER TABLE environments DROP COLUMN preco_fonte_slug")
    router.reset_init_cache()

    lido = environments_repo.get_by_slug("mm")
    assert lido["preco_trava_modo"] == "desligado"
    assert lido["preco_fonte_slug"] is None


@pytest.fixture
def setup(tmp_path, monkeypatch):
    monkeypatch.setenv("APP_DATA_DIR", str(tmp_path))
    db.set_db_path(tmp_path / "app_state.db")
    db.reset_init_cache()
    db.init()
    yield tmp_path
    db.set_db_path(None)
    db.reset_init_cache()


def _novo_env(setup):
    return environments_repo.create(
        slug="mm",
        name="MM",
        watch_dir=str(setup),
        output_dir=str(setup),
        fb_path=str(setup / "x.fdb"),
    )


def test_rota_admin_troca_modo(setup):
    from app.web.server import app

    e = _novo_env(setup)
    c = TestClient(app)
    r = c.patch(f"/api/admin/environments/{e['id']}/preco-trava", json={"modo": "observando"})
    assert r.status_code == 200
    assert r.json()["preco_trava_modo"] == "observando"
    assert environments_repo.get_by_slug("mm")["preco_trava_modo"] == "observando"

    assert (
        c.patch(
            f"/api/admin/environments/{e['id']}/preco-trava", json={"modo": "talvez"}
        ).status_code
        == 422
    )
    assert (
        c.patch(
            "/api/admin/environments/nao-existe/preco-trava", json={"modo": "ligado"}
        ).status_code
        == 404
    )


def test_rota_operador_e_403(setup, real_auth):
    from app.web.server import app

    e = _novo_env(setup)
    c = TestClient(app)
    c.post("/api/auth/bootstrap", json={"email": "admin@x.com", "password": "supersecret1"})
    c.post(
        "/api/admin/users", json={"email": "op@x.com", "password": "operpass1", "role": "operator"}
    )
    c.post("/api/auth/logout")
    c.post("/api/auth/login", json={"email": "op@x.com", "password": "operpass1"})
    r = c.patch(f"/api/admin/environments/{e['id']}/preco-trava", json={"modo": "ligado"})
    assert r.status_code == 403
    assert environments_repo.get_by_slug("mm")["preco_trava_modo"] == "desligado"
