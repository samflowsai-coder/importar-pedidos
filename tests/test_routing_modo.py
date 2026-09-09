from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.persistence import roteamento_repo, router
from app.web.server import app


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("APP_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("TEST_AUTH_BYPASS", "1")
    router.reset_init_cache()
    with router.shared_connect():
        pass
    yield TestClient(app)


def test_get_modo_de_instalacao_nova(client):
    assert client.get("/api/roteamento/modo").json()["modo"] == "desligado"


def test_put_modo_muda_sem_deploy(client):
    r = client.put("/api/roteamento/modo", json={"modo": "observando"})
    assert r.status_code == 200
    assert client.get("/api/roteamento/modo").json()["modo"] == "observando"
    assert roteamento_repo.modo() == "observando"


def test_put_modo_invalido_e_400(client):
    r = client.put("/api/roteamento/modo", json={"modo": "talvez"})
    assert r.status_code == 400
    assert roteamento_repo.modo() == "desligado"


def test_taxa_devolve_numeros_e_divergencias(client):
    roteamento_repo.registrar_sombra(
        import_id="a", degrau="documento", env_sugerido="nasmar", env_escolhido="nasmar"
    )
    roteamento_repo.registrar_sombra(
        import_id="b", degrau="documento", env_sugerido="nasmar", env_escolhido="mm"
    )
    body = client.get("/api/roteamento/taxa?dias=30").json()
    assert body["total"] == 2
    assert body["bateu"] == 1
    assert body["divergiu"] == 1
    assert [d["import_id"] for d in body["divergencias"]] == ["b"]


def test_taxa_lista_pendencias_do_watcher(client):
    roteamento_repo.registrar_pendencia(
        sha256="s1",
        source_path="/in/NBA.xlsx",
        env_scan_slug="mm",
        order_number="NBA 3",
        customer_cnpj=None,
        customer_name=None,
    )
    body = client.get("/api/roteamento/taxa").json()
    assert body["pendencias"][0]["order_number"] == "NBA 3"


def test_pagina_do_admin_responde(client):
    assert client.get("/admin/roteamento").status_code == 200


# ── Enforcement de auth real (sem TEST_AUTH_BYPASS) ─────────────────────
# Mesmo padrão de tests/test_update_routes.py: `setup` (aqui `real_client`)
# + `real_auth` do conftest desligam o bypass, então a sessão é a de verdade.


@pytest.fixture
def real_client(tmp_path, monkeypatch):
    monkeypatch.setenv("APP_DATA_DIR", str(tmp_path))
    router.reset_init_cache()
    yield TestClient(app)


def _bootstrap_operator_session(c: TestClient) -> None:
    """Cria o 1º admin, cria um operador, faz logout do admin e loga como
    o operador — deixa a sessão do client autenticada como não-admin."""
    r = c.post("/api/auth/bootstrap", json={"email": "admin@x.com", "password": "supersecret1"})
    assert r.status_code == 200, r.text
    r = c.post(
        "/api/admin/users",
        json={
            "email": "op@x.com",
            "password": "operpass1",
            "role": "operator",
        },
    )
    assert r.status_code == 201, r.text
    c.post("/api/auth/logout")
    r = c.post("/api/auth/login", json={"email": "op@x.com", "password": "operpass1"})
    assert r.status_code == 200, r.text


def test_get_modo_sem_auth_401(real_client, real_auth):
    assert real_client.get("/api/roteamento/modo").status_code == 401


def test_put_modo_sem_auth_401(real_client, real_auth):
    r = real_client.put("/api/roteamento/modo", json={"modo": "observando"})
    assert r.status_code == 401


def test_put_modo_operador_nao_admin_403(real_client, real_auth):
    _bootstrap_operator_session(real_client)
    r = real_client.put("/api/roteamento/modo", json={"modo": "observando"})
    assert r.status_code == 403
    assert roteamento_repo.modo() == "desligado"


def test_get_modo_operador_nao_admin_200(real_client, real_auth):
    """Leitura usa require_user, não require_admin — operador pode ver o modo."""
    _bootstrap_operator_session(real_client)
    assert real_client.get("/api/roteamento/modo").status_code == 200
