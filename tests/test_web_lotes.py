"""API de lotes: config, fator com vigência, prévia, fechar agora e o que ficou fora do lote."""

from __future__ import annotations

from datetime import date, datetime, timedelta
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient

from app.persistence import context as env_context
from app.persistence import environments_repo, lotes_repo, repo, router
from app.web.server import app

NASMAR = "34513679000134"
MM = "35394871000111"


def _hoje() -> date:
    return date.today()


@pytest.fixture
def portal(tmp_path, monkeypatch):
    monkeypatch.setenv("APP_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("TEST_AUTH_BYPASS", "1")
    router.reset_init_cache()
    with router.shared_connect():
        pass
    for slug, nome, cnpj in (("nasmar", "Nasmar", NASMAR), ("mm", "MM Americanense", MM)):
        environments_repo.create(
            slug=slug,
            name=nome,
            cnpj=cnpj,
            watch_dir=str(tmp_path / f"{slug}_in"),
            output_dir=str(tmp_path / f"{slug}_out"),
            fb_path=str(tmp_path / f"{slug}.fdb"),
        )
    yield TestClient(app)


def _grava(ident: str, *, reconciled_at: str | None = None, **extra) -> None:
    env = environments_repo.get_by_slug("nasmar")
    dados = {
        "id": ident,
        "source_filename": f"{ident}.pdf",
        "imported_at": datetime.now().isoformat(timespec="seconds"),
        "order_number": ident,
        "customer_name": "DAJU",
        "status": "success",
        "portal_status": "sent_to_fire",
        "snapshot": {
            "header": {
                "order_number": ident,
                "customer_name": "DAJU",
                "customer_cnpj": "11222333000181",
            },
            "items": [{"description": "MEIA", "quantity": 2.0, "unit_price": 16.12, "ean": "7891"}],
            "source_file": "",
        },
        **extra,
    }
    with env_context.active_env(env["id"], env["slug"]):
        repo.insert_import(dados)
    if reconciled_at:
        with router.env_connect("nasmar") as conn:
            conn.execute(
                "UPDATE imports SET reconciled_at = ? WHERE id = ?", (reconciled_at, ident)
            )


def _config(*, ativo: bool = True, janela: str = "semanal") -> None:
    with router.shared_connect() as conn:
        lotes_repo.save_config(
            conn,
            env_origem_slug="nasmar",
            env_espelho_slug="mm",
            cnpj_revenda=NASMAR,
            nome_revenda="Nasmar",
            janela=janela,
            dia_inicio=0,
            hora_fechamento=8,
            ativo=ativo,
            por="teste",
            agora=datetime.now() - timedelta(days=30),
        )


def _fator(valor: str = "0.07") -> None:
    with router.shared_connect() as conn:
        lotes_repo.registrar_fator(
            conn,
            env_origem_slug="nasmar",
            fator=Decimal(valor),
            modo="desconto",
            vigente_desde=_hoje() - timedelta(days=60),
            por="teste",
            agora=datetime.now() - timedelta(days=60),
        )


def _perna(ident: str, *, fire: int = 1157, com_fator: bool = True) -> int:
    _grava(ident)
    hoje = _hoje()
    inicio = hoje - timedelta(days=hoje.weekday())
    with router.shared_connect() as conn:
        lote = lotes_repo.lote_para(
            conn,
            env_origem_slug="nasmar",
            env_espelho_slug="mm",
            inicio=inicio,
            fim=inicio + timedelta(days=6),
            modo_janela="semanal",
            agora=datetime.now(),
        )
        lotes_repo.anexar_perna(
            conn,
            lote_id=lote["id"],
            import_id=ident,
            env_origem_slug="nasmar",
            fire_codigo_origem=fire,
            itens_fire=[(9001, 0)],
            pedido_cliente=ident,
            cnpj_cliente_final="11222333000181",
            razao_cliente_final="DAJU",
            agora=datetime.now(),
        )
    return lote["id"]


@pytest.fixture
def lote_com_pernas(portal):
    _config()
    _fator()
    return {"lote_id": _perna("N1"), "client": portal}


@pytest.fixture
def lote_sem_fator(portal):
    _config()
    return {"lote_id": _perna("N1"), "client": portal}


def _body_config(**kw):
    base = {
        "env_origem_slug": "nasmar",
        "env_espelho_slug": "mm",
        "nome_revenda": "Nasmar",
        "janela": "semanal",
        "dia_inicio": 0,
        "hora_fechamento": 8,
        "ativo": True,
    }
    return {**base, **kw}


# --- leitura -------------------------------------------------------------------


def test_get_lotes_sem_config(portal):
    body = portal.get("/api/lotes").json()
    assert body["config"] is None
    assert body["aberto"] is None
    assert body["recentes"] == []


def test_get_lotes_mostra_o_aberto_com_contagem_e_previa(lote_com_pernas):
    c = lote_com_pernas["client"]
    body = c.get("/api/lotes").json()
    assert body["config"]["ativo"] is True
    assert body["config"]["ativo_desde"]
    assert body["fator_vigente"]["fator"] == "0.07"
    aberto = body["aberto"]
    assert aberto["pernas"] == 1
    assert aberto["clientes"] == 1
    assert aberto["linhas_previstas"] == 1
    assert Decimal(str(aberto["total_previsto"])) == Decimal("29.98")  # 2 x 14.99
    assert aberto["modo"] == "desconto"
    assert aberto["fecha_em"]
    det = c.get(f"/api/lotes/{lote_com_pernas['lote_id']}").json()
    assert det["pernas"][0]["fire_codigo_origem"] == 1157
    assert det["previa"][0]["unit_price"] == pytest.approx(14.9916)


def test_detalhe_sem_fator_nao_tem_previa(lote_sem_fator):
    c = lote_sem_fator["client"]
    det = c.get(f"/api/lotes/{lote_sem_fator['lote_id']}").json()
    assert det["previa"] is None
    assert c.get("/api/lotes/9999").status_code == 404


def test_fora_do_lote_lista_o_que_chegou_ao_fire_sem_perna(lote_com_pernas):
    c = lote_com_pernas["client"]
    agora = datetime.now().isoformat(timespec="seconds")
    _grava("EXCEL1", sent_to_fire_at=agora)
    _grava("EXCEL2", portal_status="found_in_fire", reconciled_at=agora)
    _grava("NAOCHEGOU", portal_status="parsed")
    fora = c.get("/api/lotes").json()["fora_do_lote"]
    assert fora["total"] == 2
    assert {r["import_id"] for r in fora["itens"]} == {"EXCEL1", "EXCEL2"}
    assert "N1" not in {r["import_id"] for r in fora["itens"]}


# --- config e fator global ---------------------------------------------------------


def test_put_config_grava_com_cnpj_do_ambiente_de_origem(portal):
    r = portal.put("/api/lotes/config", json=_body_config())
    assert r.status_code == 200, r.text
    assert r.json()["cnpj_revenda"] == NASMAR
    assert r.json()["ativo_desde"]


def test_config_recusa_valores_fora_de_faixa(portal):
    assert portal.put("/api/lotes/config", json=_body_config(janela="mensal")).status_code == 422
    assert portal.put("/api/lotes/config", json=_body_config(dia_inicio=7)).status_code == 422
    assert portal.put("/api/lotes/config", json=_body_config(hora_fechamento=24)).status_code == 422


def test_config_recusa_revenda_diferente_do_intercompany_do_espelho(portal):
    mm = environments_repo.get_by_slug("mm")
    environments_repo.set_intercompany_config(
        mm["id"], cnpj="99999999000199", revenda_slug="nasmar"
    )
    r = portal.put("/api/lotes/config", json=_body_config())
    assert r.status_code == 400


def test_config_aceita_intercompany_igual_com_mascara(portal):
    mm = environments_repo.get_by_slug("mm")
    environments_repo.set_intercompany_config(
        mm["id"], cnpj="34.513.679/0001-34", revenda_slug="nasmar"
    )
    assert portal.put("/api/lotes/config", json=_body_config()).status_code == 200


def test_trocar_janela_com_lote_aberto_da_409(lote_com_pernas):
    r = lote_com_pernas["client"].put("/api/lotes/config", json=_body_config(janela="quinzenal"))
    assert r.status_code == 409


def test_fator_novo_vale_da_proxima_janela(lote_com_pernas):
    c = lote_com_pernas["client"]
    r = c.post(
        "/api/lotes/fator", json={"env_origem_slug": "nasmar", "fator": 0.07, "modo": "desconto"}
    )
    assert r.status_code == 200, r.text
    hoje = _hoje()
    proxima = hoje - timedelta(days=hoje.weekday()) + timedelta(days=7)
    assert r.json()["vigente_desde"] == proxima.isoformat()
    assert r.json()["exemplo"] == {"de": 16.12, "para": 14.99}
    # o lote da semana corrente continua com o fator congelado
    with router.shared_connect() as conn:
        assert lotes_repo.fator_vigente(conn, "nasmar", hoje)[0] == Decimal("0.07")


def test_fator_global_sem_config_da_404(portal):
    r = portal.post(
        "/api/lotes/fator", json={"env_origem_slug": "nasmar", "fator": 0.07, "modo": "desconto"}
    )
    assert r.status_code == 404


def test_fator_exemplo_no_modo_divisor(lote_com_pernas):
    r = lote_com_pernas["client"].post(
        "/api/lotes/fator", json={"env_origem_slug": "nasmar", "fator": 0.07, "modo": "divisor"}
    )
    assert r.json()["exemplo"] == {"de": 16.12, "para": 15.07}


# --- fator do lote ---------------------------------------------------------------------


def test_informar_fator_exige_modo_explicito(lote_sem_fator):
    c = lote_sem_fator["client"]
    lid = lote_sem_fator["lote_id"]
    assert c.post(f"/api/lotes/{lid}/fator", json={"fator": 0.07}).status_code == 422
    assert c.post(f"/api/lotes/{lid}/fator", json={"fator": 0.07, "modo": "x"}).status_code == 422


@pytest.mark.parametrize("fator", [-0.01, 1, 1.5])
def test_fator_fora_de_faixa_e_rejeitado(lote_sem_fator, fator):
    c = lote_sem_fator["client"]
    lid = lote_sem_fator["lote_id"]
    r = c.post(f"/api/lotes/{lid}/fator", json={"fator": fator, "modo": "desconto"})
    assert r.status_code == 422


def test_informar_fator_do_lote_so_em_aguardando_fator(lote_sem_fator):
    c = lote_sem_fator["client"]
    lid = lote_sem_fator["lote_id"]
    # ainda 'aberto': 409
    corpo = {"fator": 0.07, "modo": "desconto"}
    assert c.post(f"/api/lotes/{lid}/fator", json=corpo).status_code == 409
    assert c.post(f"/api/lotes/{lid}/fechar").status_code == 409  # marca aguardando_fator
    r = c.post(f"/api/lotes/{lid}/fator", json=corpo)
    assert r.status_code == 200, r.text
    assert r.json()["exemplo"] == {"de": 16.12, "para": 14.99}
    with router.shared_connect() as conn:
        lote = lotes_repo.get(conn, lid)
    assert lote["fator_preco_aplicado"] == "0.07"
    assert lote["fator_informado_por"] == "test@portal.local"


def test_fechar_manual_exige_fator(lote_sem_fator):
    r = lote_sem_fator["client"].post(f"/api/lotes/{lote_sem_fator['lote_id']}/fechar")
    assert r.status_code == 409
    assert "fator" in r.json()["detail"].lower()


def test_fechar_lote_vazio_da_409(portal):
    _config()
    _fator()
    hoje = _hoje()
    inicio = hoje - timedelta(days=hoje.weekday())
    with router.shared_connect() as conn:
        lote = lotes_repo.lote_para(
            conn,
            env_origem_slug="nasmar",
            env_espelho_slug="mm",
            inicio=inicio,
            fim=inicio + timedelta(days=6),
            modo_janela="semanal",
            agora=datetime.now(),
        )
    assert portal.post(f"/api/lotes/{lote['id']}/fechar").status_code == 409


def test_fechar_agora_cria_o_espelho(lote_com_pernas):
    c = lote_com_pernas["client"]
    r = c.post(f"/api/lotes/{lote_com_pernas['lote_id']}/fechar")
    assert r.status_code == 200, r.text
    assert r.json()["resultado"] == "fechado"
    espelho = r.json()["import_id_espelho"]
    mm = environments_repo.get_by_slug("mm")
    with env_context.active_env(mm["id"], mm["slug"]):
        assert repo.get_import(espelho) is not None


# --- auth real -----------------------------------------------------------------------------


@pytest.fixture
def real_client(tmp_path, monkeypatch):
    monkeypatch.setenv("APP_DATA_DIR", str(tmp_path))
    router.reset_init_cache()
    yield TestClient(app)


def _como_operador(c: TestClient) -> None:
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


def test_config_e_fator_global_exigem_admin(real_client, real_auth):
    _como_operador(real_client)
    assert real_client.put("/api/lotes/config", json=_body_config()).status_code == 403
    r = real_client.post(
        "/api/lotes/fator", json={"env_origem_slug": "nasmar", "fator": 0.07, "modo": "desconto"}
    )
    assert r.status_code == 403
    assert real_client.get("/api/lotes").status_code == 200


def test_lotes_sem_sessao_da_401(real_client, real_auth):
    assert real_client.get("/api/lotes").status_code == 401
