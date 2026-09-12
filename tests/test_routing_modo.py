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


# ── Task 15: login sem seleção de ambiente ──────────────────────────────


def test_ligado_a_home_nao_manda_escolher_ambiente(client):
    roteamento_repo.set_modo("ligado", por="t")
    r = client.get("/", follow_redirects=False)
    assert r.status_code == 200


def test_desligado_a_home_continua_mandando_escolher(client):
    roteamento_repo.set_modo("desligado", por="t")
    r = client.get("/", follow_redirects=False)
    assert r.status_code in (200, 307)
    if r.status_code == 307:
        assert r.headers["location"] == "/selecionar-ambiente"


def test_ligado_a_caixa_de_entrada_soma_os_ambientes(client, tmp_path):
    """Sem empresa escolhida e com o roteamento valendo, a lista traz as duas."""
    from app.persistence import context as env_context
    from app.persistence import environments_repo, repo

    for slug, nome in (("nasmar", "Nasmar"), ("mm", "MM Americanense")):
        environments_repo.create(
            slug=slug, name=nome, watch_dir=str(tmp_path / slug),
            output_dir=str(tmp_path / slug), fb_path=str(tmp_path / f"{slug}.fdb"),
        )
        env = environments_repo.get_by_slug(slug)
        with env_context.active_env(env["id"], env["slug"]):
            repo.insert_import({
                "id": f"{slug}-1", "source_filename": "p.pdf",
                "imported_at": "2026-09-05T10:00:00", "order_number": slug.upper(),
                "customer_name": nome, "status": "success", "portal_status": "parsed",
            })

    roteamento_repo.set_modo("ligado", por="t")
    body = client.get("/api/imported").json()
    assert {e["env_name"] for e in body["entries"]} == {"Nasmar", "MM Americanense"}
    assert body["total"] == 2
    assert body["counts"]["parsed"] == 2


# ── Fix round 1: achados da revisão ──────────────────────────────────────
# `client` roda com TEST_AUTH_BYPASS=1 — nenhum teste que use `client` prova
# nada sobre auth ou sobre o gate de `index()` (o bypass curto-circuita os
# dois `if`). Os testes abaixo usam `real_client` + `real_auth`, que desligam
# o bypass, para provar comportamento de verdade.


def _bootstrap_admin(c: TestClient) -> None:
    r = c.post("/api/auth/bootstrap", json={"email": "admin@x.com", "password": "supersecret1"})
    assert r.status_code == 200, r.text


# ACHADO 1 (CRITICAL) — `/api/imported` não declarava `Depends(require_user)`.
# Em `ligado`, sem sessão E sem cookie `portal_env`, a rota caía no ramo
# cross-env (que não depende do contexto de ambiente da request) e devolvia
# 200 com pedidos das duas empresas para um chamador anônimo. Em `desligado`/
# `observando` isso não aparecia porque a ausência de ambiente virava 412
# (`NoActiveEnvironmentError`) antes de qualquer dado vazar — um portão
# acidental, não uma checagem de auth.


def test_desligado_api_imported_exige_sessao(real_client, real_auth):
    roteamento_repo.set_modo("desligado", por="t")
    assert real_client.get("/api/imported").status_code == 401


def test_observando_api_imported_exige_sessao(real_client, real_auth):
    roteamento_repo.set_modo("observando", por="t")
    assert real_client.get("/api/imported").status_code == 401


def test_ligado_api_imported_exige_sessao(real_client, real_auth):
    """Sem o `Depends(require_user)`, este teste devolvia 200 com os pedidos
    das duas empresas para um chamador sem sessão nenhuma — a lacuna real."""
    roteamento_repo.set_modo("ligado", por="t")
    assert real_client.get("/api/imported").status_code == 401


# ACHADO 3 (Important) — nenhum teste com bypass desligado cobria o gate de
# `index()`. Os dois testes de cima (`test_*_a_home_*`) usam `client`
# (bypass ligado): `_is_test_bypass()` curto-circuita os dois `if` de
# `index()`, então eles passam igual mesmo se o ramo de `ligado` for
# apagado — provado forçando `modo()` a devolver "desligado" com o bypass
# ligado e vendo o assert passar do mesmo jeito. Os três abaixo, com sessão
# real e sem cookie de ambiente, são quem realmente prova o gate.


def test_desligado_home_sem_ambiente_redireciona_de_verdade(real_client, real_auth):
    roteamento_repo.set_modo("desligado", por="t")
    _bootstrap_admin(real_client)
    r = real_client.get("/", follow_redirects=False)
    assert r.status_code in (302, 307)
    assert "/selecionar-ambiente" in r.headers["location"]


def test_observando_home_sem_ambiente_redireciona_de_verdade(real_client, real_auth):
    roteamento_repo.set_modo("observando", por="t")
    _bootstrap_admin(real_client)
    r = real_client.get("/", follow_redirects=False)
    assert r.status_code in (302, 307)
    assert "/selecionar-ambiente" in r.headers["location"]


def test_ligado_home_sem_ambiente_nao_redireciona_de_verdade(real_client, real_auth):
    roteamento_repo.set_modo("ligado", por="t")
    _bootstrap_admin(real_client)
    r = real_client.get("/", follow_redirects=False)
    assert r.status_code == 200


# Fix round 2 desfez a carona do modo em `/api/config` (`roteamentoModo`
# tinha entrado no payload desautenticado de `/api/config` pra economizar
# uma requisição — mas `/api/roteamento/modo` exige sessão de propósito, e
# ter o mesmo valor saindo pelos dois, um gated e outro não, é a assimetria
# real). `shell.js` volta a buscar o modo por `/api/roteamento/modo`, cujo
# 401 sem sessão já está coberto por `test_get_modo_sem_auth_401` no topo
# deste arquivo.
