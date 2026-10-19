"""Envio da perna espelho ao Fire MM: a mesma trava de qualquer pedido, mais as
guardas do lote, e o rastro `lote_fiscal` do SP1 no lugar do /recebimento.

O Firebird é um dublê. O assert é sobre o SQLite (lote, reserva, `erp_lote_perna`)
e sobre o que o exportador recebeu.
"""

from __future__ import annotations

import json
from datetime import date, datetime
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient

from app.consolidators import fechamento, integracao
from app.erp import product_check
from app.erp_eventos import lote_fiscal
from app.exporters.firebird_exporter import FirebirdExportResult
from app.models.order import Order
from app.persistence import context as env_context
from app.persistence import environments_repo, erp_bases_repo, lotes_repo, repo, router
from app.web import server
from app.web.server import app

NASMAR = "34513679000134"
MM = "35394871000111"
TENANT = "dddddddd-dddd-4ddd-8ddd-dddddddddddd"
ESPELHO = "lote-NAS-2026-S34"
SEGUNDA_8H = datetime(2026, 8, 24, 8, 0)


@pytest.fixture
def portal_lote(tmp_path, monkeypatch):
    monkeypatch.setenv("APP_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("TEST_AUTH_BYPASS", "1")
    monkeypatch.setenv("PORTAL_ERP_VIGIA", "0")
    monkeypatch.setenv("PORTAL_LOTES", "0")
    router.reset_init_cache()
    with router.shared_connect():
        pass
    envs = {}
    for slug, nome, cnpj in (("nasmar", "Nasmar", NASMAR), ("mm", "MM Americanense", MM)):
        envs[slug] = environments_repo.create(
            slug=slug,
            name=nome,
            cnpj=cnpj,
            watch_dir=str(tmp_path / f"{slug}_in"),
            output_dir=str(tmp_path / f"{slug}_out"),
            fb_path=str(tmp_path / f"{slug}.fdb"),
        )
    with router.shared_connect() as conn:
        lotes_repo.save_config(
            conn,
            env_origem_slug="nasmar",
            env_espelho_slug="mm",
            cnpj_revenda=NASMAR,
            nome_revenda="Nasmar",
            janela="semanal",
            dia_inicio=0,
            hora_fechamento=8,
            ativo=True,
            por="teste",
            agora=datetime(2026, 8, 1, 9, 0),
        )
        lotes_repo.registrar_fator(
            conn,
            env_origem_slug="nasmar",
            fator=Decimal("0.07"),
            modo="desconto",
            vigente_desde=date(2026, 8, 3),
            por="teste",
            agora=datetime(2026, 8, 1, 9, 0),
        )

    from app.exporters import erp_exporter as erp_mod
    from app.exporters import firebird_exporter as fb_mod

    estado = {
        "check": {"available": False},
        "exports": [],
        "reservas_no_insert": [],
        "pushes": [],
        "resultado": None,
        "planilhas": [],
    }

    monkeypatch.setattr(
        product_check,
        "check_order",
        lambda order, *, env=None: estado["check"],  # noqa: ARG005
    )
    monkeypatch.setattr(
        erp_mod.ERPExporter,
        "export",
        lambda self, order, out: estado["planilhas"].append(order.header.order_number) or [],  # noqa: ARG005
    )

    def _export(self, order, **kw):  # noqa: ARG001
        estado["exports"].append({"order": order, **kw})
        estado["reservas_no_insert"].append(_reservas())
        if isinstance(estado["resultado"], Exception):
            raise estado["resultado"]
        if estado["resultado"] is not None:
            return estado["resultado"]
        n = len(order.items)
        return FirebirdExportResult(
            order_number=order.header.order_number,
            items_inserted=n,
            fire_codigo=7001,
            itens=[(900011 + i, i) for i in range(n)],
            produtos={i: 501 + i for i in range(n)},
        )

    monkeypatch.setattr(fb_mod.FirebirdExporter, "export", _export)
    monkeypatch.setattr(
        server,
        "push_new_order",
        lambda order, *, import_id, slug: estado["pushes"].append(import_id),  # noqa: ARG005
    )
    yield {"client": TestClient(app), "estado": estado, **envs}


@pytest.fixture
def espelho_pronto(portal_lote):
    _grava("nasmar", "N1")
    with router.shared_connect() as conn:
        lote = lotes_repo.lote_para(
            conn,
            env_origem_slug="nasmar",
            env_espelho_slug="mm",
            inicio=date(2026, 8, 17),
            fim=date(2026, 8, 23),
            modo_janela="semanal",
            agora=datetime(2026, 8, 20, 10, 0),
        )
        lotes_repo.anexar_perna(
            conn,
            lote_id=lote["id"],
            import_id="N1",
            env_origem_slug="nasmar",
            fire_codigo_origem=1157,
            itens_fire=[(9001, 0)],
            pedido_cliente="N1",
            cnpj_cliente_final="11222333000181",
            razao_cliente_final="DAJU",
            agora=datetime(2026, 8, 20, 10, 0),
        )
    r = fechamento.fechar_lote(lote["id"], por="teste", agora=SEGUNDA_8H)
    assert r["resultado"] == "fechado", r
    portal_lote["lote_id"] = lote["id"]
    return portal_lote


@pytest.fixture
def espelho_pronto_sp1(espelho_pronto):
    for slug, papel in (("nasmar", "intermediaria"), ("mm", "produtora")):
        erp_bases_repo.salvar(
            espelho_pronto[slug]["id"],
            base=slug,
            papel=papel,
            tenant_id=TENANT,
            destino_url="https://flow.test",
            foto_hora=None,
            ativo=True,
            token="fpe_" + "f" * 43,
        )
    return espelho_pronto


def _grava(slug: str, ident: str) -> None:
    env = environments_repo.get_by_slug(slug)
    dados = {
        "id": ident,
        "source_filename": f"{ident}.pdf",
        "imported_at": "2026-08-20T10:00:00",
        "order_number": ident,
        "customer_name": "DAJU",
        "status": "success",
        "portal_status": "parsed",
        "snapshot": {
            "header": {
                "order_number": ident,
                "customer_name": "DAJU",
                "customer_cnpj": "11222333000181",
            },
            "items": [{"description": "MEIA", "quantity": 2.0, "unit_price": 16.12, "ean": "7891"}],
            "source_file": "",
        },
    }
    with env_context.active_env(env["id"], env["slug"]):
        repo.insert_import(dados)


def _enviar(p, ident: str = ESPELHO, *, client: TestClient | None = None):
    c = client or p["client"]
    c.cookies.set("portal_env", p["mm"]["id"])
    return c.post(f"/api/imported/{ident}/send-to-fire")


def _lote(p) -> dict:
    with router.shared_connect() as conn:
        return lotes_repo.get(conn, p["lote_id"])


def _reservas() -> list[tuple]:
    with router.env_connect("mm") as conn:
        return [
            tuple(r) for r in conn.execute("SELECT chave_lote, base_espelho FROM erp_lote_reserva")
        ]


def _pernas_sp1() -> list[dict]:
    with router.env_connect("mm") as conn:
        return [dict(r) for r in conn.execute("SELECT * FROM erp_lote_perna").fetchall()]


def _audit(p, event_type: str) -> list[dict]:
    env = p["mm"]
    with env_context.active_env(env["id"], env["slug"]):
        return [a["detail"] for a in repo.list_audit(ESPELHO) if a["event_type"] == event_type]


# --- a trava de sempre --------------------------------------------------------


def test_espelho_passa_pelo_mesmo_is_blocking(espelho_pronto, monkeypatch):
    chamadas = []
    real = product_check.is_blocking
    monkeypatch.setattr(
        product_check, "is_blocking", lambda *a, **k: chamadas.append(1) or real(*a, **k)
    )
    assert _enviar(espelho_pronto).status_code == 200
    assert chamadas == [1]


def test_trava_de_preco_bloqueia_o_espelho_como_qualquer_pedido(espelho_pronto):
    espelho_pronto["estado"]["check"] = {
        "available": True,
        "items": [
            {
                "ean": "7891",
                "product_code": None,
                "price_status": "mismatch",
                "unit_price_order": 14.9916,
                "fire_preco_venda": 20.0,
            }
        ],
    }
    r = _enviar(espelho_pronto)
    assert r.status_code == 409
    assert "preço divergente" in r.json()["detail"]
    assert espelho_pronto["estado"]["exports"] == []
    assert _lote(espelho_pronto)["status"] == "fechado"


def test_espelho_com_item_sem_produto_no_fire_bloqueia(espelho_pronto_sp1):
    estado = espelho_pronto_sp1["estado"]
    estado["resultado"] = FirebirdExportResult(
        order_number="NAS-2026-S34",
        items_inserted=0,
        skipped=True,
        skip_reason="PRODUCT_NOT_FOUND",
    )
    r = _enviar(espelho_pronto_sp1)
    assert r.status_code == 409
    assert estado["reservas_no_insert"] == [[("NAS-2026-S34", "mm")]]
    assert _reservas() == []
    assert _lote(espelho_pronto_sp1)["status"] == "fechado"


# --- guardas do lote ----------------------------------------------------------


def _mexe_no_espelho(**campos) -> None:
    with router.env_connect("mm") as conn:
        snap = json.loads(
            conn.execute("SELECT snapshot_json FROM imports WHERE id = ?", (ESPELHO,)).fetchone()[0]
        )
        snap["items"][0].update(campos)
        conn.execute(
            "UPDATE imports SET snapshot_json = ? WHERE id = ?", (json.dumps(snap), ESPELHO)
        )


@pytest.mark.parametrize("campos", [{"quantity": 3.0}, {"unit_price": 15.0}])
def test_espelho_alterado_depois_do_fechamento_bloqueia(espelho_pronto_sp1, campos):
    _mexe_no_espelho(**campos)
    r = _enviar(espelho_pronto_sp1)
    assert r.status_code == 409
    assert "alterado depois do fechamento" in r.json()["detail"]
    assert espelho_pronto_sp1["estado"]["exports"] == []
    assert _reservas() == []
    (bloqueio,) = _audit(espelho_pronto_sp1, "send_to_fire_blocked")
    assert bloqueio["motivo"] == "lote_alterado"


def test_trocar_o_produto_do_espelho_continua_permitido(espelho_pronto):
    _mexe_no_espelho(product_code="MM-123")
    assert _enviar(espelho_pronto).status_code == 200


def test_lote_fora_de_fechado_bloqueia(espelho_pronto):
    with router.shared_connect() as conn:
        conn.execute(
            "UPDATE intercompany_lote SET status = 'erro' WHERE id = ?",
            (espelho_pronto["lote_id"],),
        )
    r = _enviar(espelho_pronto)
    assert r.status_code == 409
    (bloqueio,) = _audit(espelho_pronto, "send_to_fire_blocked")
    assert bloqueio["motivo"] == "lote_status_erro"
    assert espelho_pronto["estado"]["exports"] == []


# --- envio com sucesso --------------------------------------------------------


def test_espelho_grava_obs_e_nao_vai_para_o_recebimento(espelho_pronto):
    r = _enviar(espelho_pronto)
    assert r.status_code == 200, r.text
    (chamada,) = espelho_pronto["estado"]["exports"]
    assert chamada["obs"] == "LOTE NAS-2026-S34 | PEDIDOS NASMAR: 1157"
    assert espelho_pronto["estado"]["pushes"] == []


def test_sem_sp1_no_mm_marca_enviado_sem_lote_fiscal(espelho_pronto):
    assert _enviar(espelho_pronto).status_code == 200
    lote = _lote(espelho_pronto)
    assert lote["status"] == "enviado"
    assert lote["fire_codigo_espelho"] == 7001
    assert lote["lote_fiscal_ok"] == 0
    assert _audit(espelho_pronto, "lote_fiscal_falhou") == [{"motivo": "sp1_desligado"}]
    assert espelho_pronto["estado"]["reservas_no_insert"] == [[]]


def test_lote_fiscal_registrado_com_o_rastro_por_item(espelho_pronto_sp1):
    r = _enviar(espelho_pronto_sp1)
    assert r.status_code == 200, r.text
    assert espelho_pronto_sp1["estado"]["reservas_no_insert"] == [[("NAS-2026-S34", "mm")]]
    (perna,) = _pernas_sp1()
    assert perna["base_origem"] == "nasmar"
    assert perna["erp_codigo_origem"] == 1157
    assert perna["erp_item_codigo_origem"] == 9001
    assert perna["produto_produtor"] == 501
    assert perna["linha_lote"] == 900011
    assert Decimal(perna["quantidade"]) == Decimal("2")
    assert perna["chave_lote"] == "NAS-2026-S34"
    assert _reservas() == []
    with router.env_connect("mm") as conn:
        ev = conn.execute(
            "SELECT tipo, erp_codigo FROM erp_eventos_saida WHERE tipo = 'lote_fiscal'"
        ).fetchone()
    assert tuple(ev) == ("lote_fiscal", 7001)
    lote = _lote(espelho_pronto_sp1)
    assert lote["status"] == "enviado"
    assert lote["lote_fiscal_ok"] == 1
    assert lote["fire_codigo_espelho"] == 7001
    assert _audit(espelho_pronto_sp1, "lote_fiscal_falhou") == []


def test_insert_falhou_cancela_a_reserva(espelho_pronto_sp1):
    espelho_pronto_sp1["estado"]["resultado"] = RuntimeError("Fire caiu no meio")
    r = _enviar(espelho_pronto_sp1, client=TestClient(app, raise_server_exceptions=False))
    assert r.status_code == 500
    assert espelho_pronto_sp1["estado"]["reservas_no_insert"] == [[("NAS-2026-S34", "mm")]]
    assert _reservas() == []
    assert _lote(espelho_pronto_sp1)["status"] == "fechado"


def test_falha_no_lote_fiscal_nao_desfaz_o_envio(espelho_pronto_sp1, monkeypatch):
    def _explode(*a, **kw):
        raise lote_fiscal.PernaEmOutroLoteError("item de origem já está em outro lote")

    monkeypatch.setattr(lote_fiscal, "registrar_lote_fiscal", _explode)
    r = _enviar(espelho_pronto_sp1)
    assert r.status_code == 200, r.text
    lote = _lote(espelho_pronto_sp1)
    assert lote["status"] == "enviado"
    assert lote["lote_fiscal_ok"] == 0
    (falha,) = _audit(espelho_pronto_sp1, "lote_fiscal_falhou")
    assert "outro lote" in falha["motivo"]
    assert _reservas() == []


def test_origem_sem_sp1_nao_registra_e_cancela_a_reserva(espelho_pronto_sp1):
    with router.shared_connect() as conn:
        conn.execute(
            "DELETE FROM erp_bases WHERE environment_id = ?", (espelho_pronto_sp1["nasmar"]["id"],)
        )
    assert _enviar(espelho_pronto_sp1).status_code == 200
    assert _pernas_sp1() == []
    assert _reservas() == []
    assert _lote(espelho_pronto_sp1)["lote_fiscal_ok"] == 0
    (falha,) = _audit(espelho_pronto_sp1, "lote_fiscal_falhou")
    assert falha["motivo"] == "sp1_origem_desligado"


# --- o resto não muda ---------------------------------------------------------


def test_pedido_comum_nao_muda(portal_lote):
    _grava("mm", "M1")
    r = _enviar(portal_lote, "M1")
    assert r.status_code == 200, r.text
    (chamada,) = portal_lote["estado"]["exports"]
    assert chamada["obs"] is None
    assert portal_lote["estado"]["pushes"] == ["M1"]
    assert integracao.e_espelho("M1") is False


def test_espelho_nao_sai_por_planilha(espelho_pronto):
    assert integracao.e_espelho(ESPELHO) is True
    c = espelho_pronto["client"]
    c.cookies.set("portal_env", espelho_pronto["mm"]["id"])
    r = c.post(f"/api/imported/{ESPELHO}/export-xlsx")
    assert r.status_code == 409
    assert "EXPORT_MODE=db" in r.json()["detail"]


# --- rodada de correção 1 -------------------------------------------------------


def test_excecao_depois_do_insert_nao_perde_o_lote_fiscal(espelho_pronto_sp1, monkeypatch):
    def _explode(*a, **kw):
        raise RuntimeError("estado não andou")

    monkeypatch.setattr(server, "transition", _explode)
    r = _enviar(espelho_pronto_sp1, client=TestClient(app, raise_server_exceptions=False))
    assert r.status_code == 500
    lote = _lote(espelho_pronto_sp1)
    assert lote["status"] == "enviado"
    assert lote["lote_fiscal_ok"] == 1
    assert lote["fire_codigo_espelho"] == 7001
    (perna,) = _pernas_sp1()
    assert perna["linha_lote"] == 900011
    assert _reservas() == []


def test_base_sp1_desligada_conta_como_sem_sp1(espelho_pronto_sp1):
    erp_bases_repo.salvar(
        espelho_pronto_sp1["mm"]["id"],
        base="mm",
        papel="produtora",
        tenant_id=TENANT,
        destino_url="https://flow.test",
        foto_hora=None,
        ativo=False,
    )
    assert _enviar(espelho_pronto_sp1).status_code == 200
    assert espelho_pronto_sp1["estado"]["reservas_no_insert"] == [[]]
    assert _pernas_sp1() == []
    assert _lote(espelho_pronto_sp1)["lote_fiscal_ok"] == 0
    assert _audit(espelho_pronto_sp1, "lote_fiscal_falhou") == [{"motivo": "sp1_desligado"}]


def test_espelho_fora_do_ambiente_espelho_bloqueia(espelho_pronto_sp1):
    env = espelho_pronto_sp1["mm"]
    with env_context.active_env(env["id"], env["slug"]):
        order = Order.model_validate(repo.get_import(ESPELHO)["snapshot"])
    r = integracao.preparar_envio_espelho(ESPELHO, order, {"slug": "nasmar"})
    assert isinstance(r, integracao.BloqueioEspelho)
    assert r.motivo == "ambiente_errado"
    assert _reservas() == []


def test_modo_both_nao_gera_planilha_do_espelho(espelho_pronto, monkeypatch):
    monkeypatch.setenv("EXPORT_MODE", "both")
    _grava("mm", "M1")
    assert _enviar(espelho_pronto).status_code == 200
    assert _enviar(espelho_pronto, "M1").status_code == 200
    assert espelho_pronto["estado"]["planilhas"] == ["M1"]
    assert len(espelho_pronto["estado"]["exports"]) == 2


def test_falha_ao_concluir_deixa_rastro_na_auditoria(espelho_pronto, monkeypatch):
    def _explode(*a, **kw):
        raise RuntimeError("banco compartilhado caiu")

    monkeypatch.setattr(lotes_repo, "marcar_enviado", _explode)
    assert _enviar(espelho_pronto).status_code == 200
    (falha,) = _audit(espelho_pronto, "lote_fiscal_falhou")
    assert "banco compartilhado caiu" in falha["motivo"]
