"""Escopo do item para o vínculo de produto (rede:<id> / raiz:<8>)."""

from __future__ import annotations

import pytest

from app.erp import produto_escopo
from app.models.order import Order, OrderHeader, OrderItem
from app.persistence import precos_repo, router

MM = "11111111000191"
GERADO = "2026-10-20T11:00:00.000Z"


@pytest.fixture
def conn(tmp_path, monkeypatch):
    monkeypatch.setenv("APP_DATA_DIR", str(tmp_path))
    router.reset_init_cache()
    with router.shared_connect():
        pass
    with router.env_connect("mm") as c:
        yield c


def _pedido(cab: str | None, *entregas: str | None) -> Order:
    return Order(
        header=OrderHeader(customer_cnpj=cab),
        items=[OrderItem(product_code=f"P{i}", delivery_cnpj=e) for i, e in enumerate(entregas)],
    )


def test_cnpj_do_item_cabecalho_vence_entrega():
    o = _pedido("33.200.056/0001-00", "06.347.409/0001-11")
    assert produto_escopo.cnpj_do_item(o, 0) == "33200056000100"


def test_cnpj_do_item_sem_cabecalho_usa_entrega_do_item():
    o = _pedido(None, "33.200.056/0313-70")
    assert produto_escopo.cnpj_do_item(o, 0) == "33200056031370"


def test_cnpj_real_intercompany_vence_cabecalho():
    o = _pedido("33.200.056/0001-00", "06.347.409/0001-11")
    assert (
        produto_escopo.cnpj_do_item(o, 0, cnpj_real="06.347.409/0001-11") == "06347409000111"
    )


def test_cnpj_do_item_invalido_ou_ausente_devolve_none():
    assert produto_escopo.cnpj_do_item(_pedido("123", None), 0) is None
    assert produto_escopo.cnpj_do_item(_pedido(None, None), 0) is None
    assert produto_escopo.cnpj_do_item(_pedido(None, "x"), 5) is None  # índice fora


def test_cabecalho_invalido_cai_no_proximo():
    o = _pedido("123", "33.200.056/0313-70")
    assert produto_escopo.cnpj_do_item(o, 0) == "33200056031370"


def test_escopos_por_item_em_pedido_misto(conn):
    o = _pedido(None, "33.200.056/0313-70", "06.347.409/0001-11")
    c0 = produto_escopo.cnpj_do_item(o, 0)
    c1 = produto_escopo.cnpj_do_item(o, 1)
    assert produto_escopo.escopos_para(conn, c0) == ["raiz:33200056"]
    assert produto_escopo.escopos_para(conn, c1) == ["raiz:06347409"]


def test_escopos_com_rede_e_sem_rede(conn):
    pagina = {
        "schema": "precos.tabela.v1",
        "capacidades": ["precos.tabela.v1", "precos.aprovacao.v1"],
        "geradoEm": GERADO,
        "empresaFaturadoraCnpj": MM,
        "reiniciar": False,
        "mudancas": [
            {
                "tipo": "rede",
                "redeId": "r1",
                "nome": "Rede r1",
                "ativa": True,
                "travada": False,
                "raizes": ["33200056"],
                "cnpjs": [],
            }
        ],
        "cursor": "c1",
        "temMais": False,
    }
    precos_repo.aplicar_pagina(conn, pagina, empresa_cnpj=MM)
    assert produto_escopo.escopos_para(conn, "33200056031370") == ["rede:r1", "raiz:33200056"]
    assert produto_escopo.escopo_de_gravacao(conn, "33200056031370") == "rede:r1"
    assert produto_escopo.escopos_para(conn, "06347409000111") == ["raiz:06347409"]
    assert produto_escopo.escopo_de_gravacao(conn, "06347409000111") == "raiz:06347409"


def test_sem_cnpj_sem_escopo(conn):
    assert produto_escopo.escopos_para(conn, None) == []
    assert produto_escopo.escopo_de_gravacao(conn, None) is None
