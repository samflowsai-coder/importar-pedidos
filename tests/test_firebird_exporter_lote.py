"""Exportador do Fire para o lote intercompany: OBS no cabeçalho e produto por linha.

Mocka o cursor do Firebird — não toca .fdb. A OBS do espelho carrega a lista
de pedidos de origem; o produto gravado em cada linha (`produtos`) é o que o
fechamento do lote e o `lote_fiscal` do SP1 leem depois.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from app.erp import queries
from app.exporters.firebird_exporter import FirebirdExporter
from app.models.order import Order, OrderHeader, OrderItem


def _order() -> Order:
    return Order(
        header=OrderHeader(
            order_number="NAS-2026-S34",
            customer_name="NASMAR LTDA",
            customer_cnpj="34.513.679/0001-34",
        ),
        items=[
            OrderItem(description="KIT A", quantity=2, unit_price=25.5, ean="7891234567890"),
            OrderItem(description="MEIA B", quantity=1, unit_price=10.0, ean="7899999999999"),
        ],
    )


@pytest.fixture
def fake_fb(monkeypatch):
    """Exporter com cursor falso: 2 itens, produtos 501 (KIT) e 502 (UN)."""
    cur = MagicMock()
    cur.fetchone.side_effect = [
        (4242,),  # FIND_CLIENT_BY_CNPJ
        (0,),  # CHECK_ORDER_EXISTS
        (100,),  # GET_NEXT_CABVENDAS_CODIGO
        (501, "KIT A", 0, "KIT"),  # FIND_PRODUCT_BY_EAN (item 0)
        (200,),  # GET_NEXT_CORPOVENDAS_CODIGO
        (502, "MEIA B", 0, "UN"),  # FIND_PRODUCT_BY_EAN (item 1)
        (201,),  # GET_NEXT_CORPOVENDAS_CODIGO
    ]
    exporter = FirebirdExporter()
    fake_conn = MagicMock()
    fake_conn.cursor.return_value = cur

    class _CtxConn:
        def __enter__(self):  # noqa: N805
            return fake_conn

        def __exit__(self, *a):  # noqa: N805
            return False

    monkeypatch.setattr(exporter, "_conn", MagicMock())
    exporter._conn.is_configured.return_value = True
    exporter._conn.connect.return_value = _CtxConn()
    return exporter, cur


def _obs_do_insert(cur: MagicMock):
    (params,) = [
        c.args[1] for c in cur.execute.call_args_list if c.args[0] == queries.INSERT_CAB_VENDAS
    ]
    return params[6]  # OBS: 7o elemento da tupla de order_to_cabvendas


def test_obs_chega_no_insert_do_cabecalho(fake_fb):
    exporter, cur = fake_fb
    obs = "LOTE NAS-2026-S34 | PEDIDOS NASMAR: 1157"

    result = exporter.export(_order(), obs=obs)

    assert result.skipped is False
    assert _obs_do_insert(cur) == obs


def test_sem_obs_continua_none(fake_fb):
    exporter, cur = fake_fb

    exporter.export(_order())

    assert _obs_do_insert(cur) is None


def test_resultado_traz_o_produto_de_cada_posicao(fake_fb):
    exporter, _ = fake_fb

    result = exporter.export(_order())

    assert result.produtos == {0: 501, 1: 502}
    assert result.itens == [(200, 0), (201, 1)]
    assert "produtos" not in result.to_dict()
