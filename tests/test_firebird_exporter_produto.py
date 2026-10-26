"""Resolução do produto no insert direto do Fire (EXPORT_MODE=db/both).

Mocka o cursor do Firebird — não toca .fdb. Cobre o BACKLOG §2.1: item
casado por vínculo de-para entra pelo SEQ do vínculo, nunca relido por
EAN/CODPROD_ALTERN (o `fire_codigo` do de-para é `str(SEQ)`, que relido
como código alternativo casa o produto errado ou nada).
"""

from __future__ import annotations

from unittest.mock import MagicMock

from app.erp import queries
from app.exporters.firebird_exporter import FirebirdExporter
from app.models.order import Order, OrderHeader, OrderItem


def _order() -> Order:
    return Order(
        header=OrderHeader(
            order_number="DEP-1",
            customer_name="ACME LTDA",
            customer_cnpj="11.222.333/0001-44",
        ),
        items=[
            OrderItem(description="KIT A", quantity=2, unit_price=25.5, product_code="1234"),
        ],
    )


def _exporter_with_fake_cursor(monkeypatch, cursor: MagicMock) -> FirebirdExporter:
    exporter = FirebirdExporter()
    fake_conn = MagicMock()
    fake_conn.cursor.return_value = cursor

    class _CtxConn:
        def __enter__(self):  # noqa: N805
            return fake_conn

        def __exit__(self, *a):  # noqa: N805
            return False

    monkeypatch.setattr(exporter, "_conn", MagicMock())
    # A figura tem teste próprio (test_figura_fiscal.py); aqui o perfil passa intacto.
    monkeypatch.setattr("app.erp.figura_fiscal.resolver", lambda cur, cliente, perfil: perfil)
    exporter._conn.is_configured.return_value = True
    exporter._conn.connect.return_value = _CtxConn()
    return exporter


def _seqs_inseridos(exporter: FirebirdExporter, monkeypatch) -> list:
    """Espiona o product_seq que vai para cada INSERT de CORPO_VENDAS."""
    capturados: list = []
    original = exporter._mapper.item_to_corpovendas

    def _spy(row, item_pk, header_pk, product_seq, **kw):
        capturados.append(product_seq)
        return original(row, item_pk, header_pk, product_seq, **kw)

    monkeypatch.setattr(exporter._mapper, "item_to_corpovendas", _spy)
    return capturados


def test_item_vinculado_entra_pelo_seq_do_depara(monkeypatch):
    cur = MagicMock()
    cur.fetchone.side_effect = [
        (4242,),  # FIND_CLIENT_BY_CNPJ
        (0,),  # CHECK_ORDER_EXISTS
        (100,),  # GET_NEXT_CABVENDAS_CODIGO
        (1234, "KIT A", 0, "KIT"),  # FIND_PRODUCT_BY_SEQ
        (200,),  # GET_NEXT_CORPOVENDAS_CODIGO
    ]
    exporter = _exporter_with_fake_cursor(monkeypatch, cur)
    seqs = _seqs_inseridos(exporter, monkeypatch)

    result = exporter.export(_order(), produtos_por_item={0: 1234})

    assert result.skipped is False
    assert result.items_inserted == 1
    assert seqs == [1234]
    sql = [c.args[0] for c in cur.execute.call_args_list]
    assert queries.FIND_PRODUCT_BY_SEQ in sql
    # Nunca relê o SEQ como código alternativo nem por EAN.
    assert queries.FIND_PRODUCT_BY_CODE not in sql
    assert queries.FIND_PRODUCT_BY_EAN not in sql


def test_seq_do_depara_sumiu_do_fire_recusa_o_pedido(monkeypatch):
    cur = MagicMock()
    cur.fetchone.side_effect = [
        (4242,),  # FIND_CLIENT_BY_CNPJ
        (0,),  # CHECK_ORDER_EXISTS
        (100,),  # GET_NEXT_CABVENDAS_CODIGO
        None,  # FIND_PRODUCT_BY_SEQ — produto apagado/inativado no Fire
    ]
    exporter = _exporter_with_fake_cursor(monkeypatch, cur)

    result = exporter.export(_order(), produtos_por_item={0: 1234})

    assert result.skipped is True
    assert result.skip_reason == "PRODUCT_NOT_FOUND"
    sql = [c.args[0] for c in cur.execute.call_args_list]
    assert queries.INSERT_CORPO_VENDAS not in sql


def test_item_sem_produto_no_fire_recusa_o_pedido(monkeypatch):
    """Antes: item sem match entrava em CORPO_VENDAS com CODPRODUTO NULL e só
    um warning no log. Agora o pedido inteiro é recusado (a conexão faz
    rollback do cabeçalho já inserido)."""
    order = Order(
        header=OrderHeader(
            order_number="DEP-2",
            customer_name="ACME LTDA",
            customer_cnpj="11.222.333/0001-44",
        ),
        items=[
            OrderItem(
                description="X", quantity=1, unit_price=10.0, ean="789000", product_code="NAO-EXISTE"
            ),
        ],
    )
    cur = MagicMock()
    cur.fetchone.side_effect = [
        (4242,),  # FIND_CLIENT_BY_CNPJ
        (0,),  # CHECK_ORDER_EXISTS
        (100,),  # GET_NEXT_CABVENDAS_CODIGO
        None,  # FIND_PRODUCT_BY_EAN
        None,  # FIND_PRODUCT_BY_CODE
    ]
    exporter = _exporter_with_fake_cursor(monkeypatch, cur)

    result = exporter.export(order)

    assert result.skipped is True
    assert result.skip_reason == "PRODUCT_NOT_FOUND"
    sql = [c.args[0] for c in cur.execute.call_args_list]
    assert queries.INSERT_CORPO_VENDAS not in sql
