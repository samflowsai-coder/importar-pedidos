from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.persistence import environments_repo, roteamento_decisao_repo, router
from app.persistence import regra_faturamento_repo as repo
from app.web.server import app


def _envs():
    for slug in ("mm", "nasmar"):
        environments_repo.create(
            slug=slug, name=slug.upper(), watch_dir=f"/x/{slug}/in",
            output_dir=f"/x/{slug}/out", fb_path=f"/x/{slug}.fdb",
        )


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("APP_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("TEST_AUTH_BYPASS", "1")
    router.reset_init_cache()
    with router.shared_connect():
        pass
    _envs()
    yield TestClient(app)


@pytest.fixture
def real_client(tmp_path, monkeypatch):
    monkeypatch.setenv("APP_DATA_DIR", str(tmp_path))
    router.reset_init_cache()
    with router.shared_connect():
        pass
    _envs()
    yield TestClient(app)


def _operador(c: TestClient) -> None:
    r = c.post("/api/auth/bootstrap", json={"email": "admin@x.com", "password": "supersecret1"})
    assert r.status_code == 200, r.text
    r = c.post(
        "/api/admin/users",
        json={"email": "op@x.com", "password": "operpass1", "role": "operator"},
    )
    assert r.status_code == 201, r.text
    c.post("/api/auth/logout")
    r = c.post("/api/auth/login", json={"email": "op@x.com", "password": "operpass1"})
    assert r.status_code == 200, r.text


BASE = "/api/roteamento/regra"
REDE = {"nome": "Nova", "env_slug": "mm", "chaves": [{"tipo": "raiz", "chave": "12345678"}]}


def test_get_devolve_as_4_redes_da_semente(client):
    body = client.get(BASE).json()
    assert [r["nome"] for r in body["redes"]] == ["Centauro", "Kallan", "Riachuelo", "Sam's Club"]
    assert all(r["chaves"] for r in body["redes"])
    assert sorted(e["slug"] for e in body["empresas"]) == ["mm", "nasmar"]
    assert body["config"] is None or body["config"]["ativa"] is False


def test_put_config_grava(client):
    r = client.put(BASE, json={"env_sp": "mm", "env_fora_sp": "nasmar", "ativa": True})
    assert r.status_code == 200, r.text
    cfg = repo.config()
    assert cfg["ativa"] is True and cfg["env_fora_sp"] == "nasmar"


def test_put_config_erro_do_repo_e_400_com_mensagem(client):
    r = client.put(BASE, json={"env_sp": "mm", "env_fora_sp": "mm", "ativa": False})
    assert r.status_code == 400
    assert "diferentes" in r.json()["detail"]


def test_post_rede_cria_e_put_edita(client):
    r = client.post(f"{BASE}/redes", json=REDE)
    assert r.status_code == 200, r.text
    rid = r.json()["id"]
    r = client.put(f"{BASE}/redes/{rid}", json={**REDE, "nome": "Outra"})
    assert r.status_code == 200, r.text
    assert "Outra" in [x["nome"] for x in repo.listar_redes()]


def test_chave_invalida_e_400(client):
    body = {**REDE, "chaves": [{"tipo": "raiz", "chave": "123"}]}
    r = client.post(f"{BASE}/redes", json=body)
    assert r.status_code == 400
    assert "8 dígitos" in r.json()["detail"]


def test_tipo_fora_do_enum_e_422(client):
    body = {**REDE, "chaves": [{"tipo": "x", "chave": "12345678"}]}
    assert client.post(f"{BASE}/redes", json=body).status_code == 422


def test_nome_repetido_e_400_ptbr(client):
    client.post(f"{BASE}/redes", json=REDE)
    outra = {**REDE, "chaves": [{"tipo": "raiz", "chave": "87654321"}]}
    r = client.post(f"{BASE}/redes", json=outra)
    assert r.status_code == 400
    assert r.json()["detail"] == "Já existe uma rede com esse nome."


def test_env_slug_inexistente_ou_inativo_e_400(client):
    r = client.post(f"{BASE}/redes", json={**REDE, "env_slug": "fantasma"})
    assert r.status_code == 400
    assert "fantasma" in r.json()["detail"]


def test_put_e_delete_de_rede_inexistente_sao_404(client):
    assert client.put(f"{BASE}/redes/9999", json=REDE).status_code == 404
    assert client.delete(f"{BASE}/redes/9999").status_code == 404


def test_delete_remove_a_rede(client):
    rid = client.post(f"{BASE}/redes", json=REDE).json()["id"]
    assert client.delete(f"{BASE}/redes/{rid}").status_code == 200
    assert "Nova" not in [x["nome"] for x in repo.listar_redes()]


def test_decisoes_por_import_em_ordem(client):
    for acao in ("automatico", "observando"):
        roteamento_decisao_repo.registrar(
            import_id="imp1", modo="ligado", degrau="regra", env_sugerido="mm",
            env_escolhido="mm", decidido_por="t", motivo=None, acao=acao,
        )
    body = client.get("/api/roteamento/decisoes?import_id=imp1").json()
    assert [d["acao"] for d in body] == ["automatico", "observando"]


def test_operador_comum_nao_escreve_mas_le(real_client, real_auth):
    _operador(real_client)
    assert real_client.get(BASE).status_code == 200
    assert real_client.get("/api/roteamento/decisoes?import_id=x").status_code == 200
    assert real_client.put(
        BASE, json={"env_sp": "mm", "env_fora_sp": "nasmar", "ativa": False}
    ).status_code == 403
    assert real_client.post(f"{BASE}/redes", json=REDE).status_code == 403
    assert real_client.put(f"{BASE}/redes/1", json=REDE).status_code == 403
    assert real_client.delete(f"{BASE}/redes/1").status_code == 403


def test_rede_sem_chave_com_regra_ligada_e_400(client):
    assert client.put(BASE, json={"env_sp": "mm", "env_fora_sp": "nasmar", "ativa": True}).status_code == 200
    rid = next(r["id"] for r in repo.listar_redes() if r["nome"] == "Kallan")
    r = client.put(f"{BASE}/redes/{rid}", json={"nome": "Kallan", "env_slug": "mm", "chaves": []})
    assert r.status_code == 400
    assert r.json()["detail"] == "Com a regra ligada, toda rede precisa de ao menos uma chave."


def test_delete_com_regra_ligada_e_400(client):
    assert client.put(BASE, json={"env_sp": "mm", "env_fora_sp": "nasmar", "ativa": True}).status_code == 200
    rid = next(r["id"] for r in repo.listar_redes() if r["nome"] == "Kallan")
    r = client.delete(f"{BASE}/redes/{rid}")
    assert r.status_code == 400
    assert r.json()["detail"] == "Desligue a regra antes de remover uma rede."
    assert "Kallan" in [x["nome"] for x in repo.listar_redes()]
