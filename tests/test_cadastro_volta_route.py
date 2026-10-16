"""Rota GET /{env_id}/flowpcp/cadastro-volta."""

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.persistence import cadastro_volta_repo, db, environments_repo, router


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


def _get(env_id):
    from app.web.server import app

    return TestClient(app).get(f"/api/admin/environments/{env_id}/flowpcp/cadastro-volta")


def _env(setup):
    return environments_repo.create(
        slug="mm",
        name="MM",
        watch_dir=str(setup),
        output_dir=str(setup),
        fb_path=str(setup / "x.fdb"),
    )


def test_404(setup):
    assert _get("nao-existe").status_code == 404


def test_livro_vazio(setup):
    r = _get(_env(setup)["id"])
    assert r.status_code == 200
    assert r.json() == {"modo": "desligado", "por_estado": {}, "ultimos": []}


def test_duas_linhas_com_plano_truncado(setup):
    env = _env(setup)
    with router.env_connect("mm") as conn:
        for i, plano in enumerate(['{"a":1}', "x" * 5000]):
            cadastro_volta_repo.registrar(
                conn,
                cadastro_id=f"id{i}",
                versao=1,
                estado="simulado",
                acao="criar",
                tipo="kit",
                erro_codigo=None,
                erro_mensagem=None,
                plano_json=plano,
            )
    body = _get(env["id"]).json()
    assert body["por_estado"] == {"simulado": 2}
    por_id = {u["cadastro_id"]: u for u in body["ultimos"]}
    assert por_id["id0"]["plano_truncado"] is False
    assert por_id["id1"]["plano_truncado"] is True and len(por_id["id1"]["plano_json"]) == 2048


def test_plano_truncado_por_bytes_com_acento(setup):
    env = _env(setup)
    with router.env_connect("mm") as conn:
        cadastro_volta_repo.registrar(
            conn,
            cadastro_id="id0",
            versao=1,
            estado="simulado",
            acao="criar",
            tipo="kit",
            erro_codigo=None,
            erro_mensagem=None,
            plano_json="ç" * 1500,
        )
    (linha,) = _get(env["id"]).json()["ultimos"]
    assert linha["plano_truncado"] is True
    assert len(linha["plano_json"].encode("utf-8")) <= 2048


def test_sem_login_e_401(setup, real_auth):
    assert _get(_env(setup)["id"]).status_code == 401


def test_operador_logado_e_403(setup, real_auth):
    from app.web.server import app

    env = _env(setup)
    c = TestClient(app)
    c.post("/api/auth/bootstrap", json={"email": "admin@x.com", "password": "supersecret1"})
    c.post(
        "/api/admin/users", json={"email": "op@x.com", "password": "operpass1", "role": "operator"}
    )
    c.post("/api/auth/logout")
    assert (
        c.post("/api/auth/login", json={"email": "op@x.com", "password": "operpass1"}).status_code
        == 200
    )
    assert c.get(f"/api/admin/environments/{env['id']}/flowpcp/cadastro-volta").status_code == 403
