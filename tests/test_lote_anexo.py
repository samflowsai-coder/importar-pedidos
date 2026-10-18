"""Perna de origem (pedido Nasmar enviado ao Fire) entra no lote da semana.

O Firebird é um dublê: o assert é sobre o que fica no SQLite compartilhado
(`intercompany_lote_item`), nunca sobre um insert real.
"""

from __future__ import annotations

import json
from datetime import datetime

import pytest
from fastapi.testclient import TestClient

from app.exporters.firebird_exporter import FirebirdExportResult
from app.persistence import context as env_context
from app.persistence import environments_repo, lotes_repo, repo, router
from app.web.server import app

NASMAR = "34513679000134"
MM = "35394871000111"


@pytest.fixture
def portal_lote(tmp_path, monkeypatch):
    monkeypatch.setenv("APP_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("TEST_AUTH_BYPASS", "1")
    router.reset_init_cache()
    with router.shared_connect():
        pass
    nasmar = environments_repo.create(
        slug="nasmar",
        name="Nasmar",
        cnpj=NASMAR,
        watch_dir=str(tmp_path / "n_in"),
        output_dir=str(tmp_path / "n_out"),
        fb_path=str(tmp_path / "n.fdb"),
    )
    mm = environments_repo.create(
        slug="mm",
        name="MM Americanense",
        cnpj=MM,
        watch_dir=str(tmp_path / "m_in"),
        output_dir=str(tmp_path / "m_out"),
        fb_path=str(tmp_path / "m.fdb"),
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

    from app.erp import product_check as pc_mod
    from app.exporters import erp_exporter as erp_mod
    from app.exporters import firebird_exporter as fb_mod

    monkeypatch.setattr(
        pc_mod,
        "check_order",
        lambda order, *, env=None: {"available": False},  # noqa: ARG005
    )
    monkeypatch.setattr(erp_mod.ERPExporter, "export", lambda self, order, out: [])  # noqa: ARG005

    def _export(self, order, **kw):  # noqa: ARG001
        return FirebirdExportResult(
            order_number=order.header.order_number,
            items_inserted=1,
            fire_codigo=1157,
            itens=[(9001, 0)],
            produtos={0: 501},
        )

    monkeypatch.setattr(fb_mod.FirebirdExporter, "export", _export)
    yield {"client": TestClient(app), "nasmar": nasmar, "mm": mm}


def _grava(slug: str, ident: str, imported_at: str = "2026-08-20T10:00:00") -> None:
    env = environments_repo.get_by_slug(slug)
    dados = {
        "id": ident,
        "source_filename": f"{ident}.pdf",
        "imported_at": imported_at,
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
            "items": [{"description": "MEIA", "quantity": 2.0, "unit_price": 9.9, "ean": "7891"}],
            "source_file": "",
        },
    }
    with env_context.active_env(env["id"], env["slug"]):
        repo.insert_import(dados)


def _envia(portal_lote, slug: str, ident: str):
    c = portal_lote["client"]
    c.cookies.set("portal_env", portal_lote[slug]["id"])
    return c.post(f"/api/imported/{ident}/send-to-fire")


def _pernas() -> list[dict]:
    with router.shared_connect() as conn:
        rows = conn.execute(
            """
            SELECT l.chave_lote, i.import_id, i.fire_codigo_origem, i.itens_fire_json,
                   i.pedido_cliente, i.cnpj_cliente_final, i.razao_cliente_final
            FROM intercompany_lote_item i JOIN intercompany_lote l ON l.id = i.lote_id
            """
        ).fetchall()
    cols = ("chave", "import_id", "fire", "itens", "pedido", "cnpj", "razao")
    return [dict(zip(cols, r, strict=True)) for r in rows]


def test_perna_enviada_ao_fire_entra_no_lote_da_semana_da_importacao(portal_lote):
    _grava("nasmar", "N1")  # importado quinta 2026-08-20; enviado hoje

    r = _envia(portal_lote, "nasmar", "N1")

    assert r.status_code == 200, r.text
    (perna,) = _pernas()
    assert perna["chave"] == "NAS-2026-S34"
    assert perna["import_id"] == "N1"
    assert perna["fire"] == 1157
    assert json.loads(perna["itens"]) == [[9001, 0]]
    assert perna["pedido"] == "N1"
    assert perna["cnpj"] == "11222333000181"
    assert perna["razao"] == "DAJU"


def test_perna_de_janela_fechada_vai_para_a_proxima(portal_lote):
    from datetime import date

    with router.shared_connect() as conn:
        lote = lotes_repo.lote_para(
            conn,
            env_origem_slug="nasmar",
            env_espelho_slug="mm",
            inicio=date(2026, 8, 17),
            fim=date(2026, 8, 23),
            modo_janela="semanal",
            agora=datetime(2026, 8, 17, 9, 0),
        )
        conn.execute("UPDATE intercompany_lote SET status = 'fechado' WHERE id = ?", (lote["id"],))
    _grava("nasmar", "N1")

    r = _envia(portal_lote, "nasmar", "N1")

    assert r.status_code == 200, r.text
    (perna,) = _pernas()
    assert perna["chave"] == "NAS-2026-S35"


def test_falha_ao_anexar_nao_derruba_o_envio(portal_lote, monkeypatch):
    def _explode(*a, **kw):
        raise RuntimeError("banco caiu")

    monkeypatch.setattr(lotes_repo, "anexar_perna", _explode)
    _grava("nasmar", "N1")

    r = _envia(portal_lote, "nasmar", "N1")

    assert r.status_code == 200, r.text
    assert r.json()["fire_codigo"] == 1157
    assert _pernas() == []


def test_ambiente_sem_config_ou_config_inativa_nao_anexa(portal_lote):
    _grava("mm", "M1")
    assert _envia(portal_lote, "mm", "M1").status_code == 200
    assert _pernas() == []

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
            ativo=False,
            por="teste",
            agora=datetime(2026, 8, 1, 9, 0),
        )
    _grava("nasmar", "N1")
    assert _envia(portal_lote, "nasmar", "N1").status_code == 200
    assert _pernas() == []


def test_sem_lote_aberto_em_8_janelas_desiste_sem_deixar_lote_vazio(portal_lote, monkeypatch):
    from app.consolidators import integracao
    from app.models.order import Order, OrderHeader

    def _sempre_fechado(conn, *, lote_id, **kw):  # noqa: ARG001
        raise lotes_repo.LoteFechadoError(str(lote_id))

    monkeypatch.setattr(lotes_repo, "anexar_perna", _sempre_fechado)
    order = Order(header=OrderHeader(order_number="N1"), items=[])
    result = FirebirdExportResult(order_number="N1", items_inserted=1, fire_codigo=1157)

    lote_id = integracao.anexar_perna_de_origem(
        "N1",
        {"imported_at": "2026-08-20T10:00:00"},
        order,
        result,
        {"slug": "nasmar"},
        agora=datetime(2026, 8, 25, 9, 0),
    )

    assert lote_id is None
    with router.shared_connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM intercompany_lote").fetchone()[0] == 0


def test_espelho_nunca_e_anexado_como_perna(portal_lote):
    from datetime import date

    _grava("nasmar", "lote-NAS-2026-S33")
    with router.shared_connect() as conn:
        lote = lotes_repo.lote_para(
            conn,
            env_origem_slug="nasmar",
            env_espelho_slug="nasmar",
            inicio=date(2026, 8, 10),
            fim=date(2026, 8, 16),
            modo_janela="semanal",
            agora=datetime(2026, 8, 10, 9, 0),
        )
        conn.execute(
            "UPDATE intercompany_lote SET status = 'fechado', import_id_espelho = ? WHERE id = ?",
            ("lote-NAS-2026-S33", lote["id"]),
        )

    from app.consolidators import integracao
    from app.models.order import Order, OrderHeader

    entry = {"imported_at": "2026-08-12T10:00:00"}
    order = Order(header=OrderHeader(order_number="E", customer_name="X"), items=[])
    result = FirebirdExportResult("E", 1, fire_codigo=1170, itens=[(9001, 0)])

    lote_id = integracao.anexar_perna_de_origem(
        "lote-NAS-2026-S33", entry, order, result, {"slug": "nasmar"}
    )

    assert lote_id is None
    assert _pernas() == []
