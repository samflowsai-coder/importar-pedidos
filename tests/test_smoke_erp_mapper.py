"""Smoke tests for app.erp.mapper (FireSistemasMapper).

Pure unit tests on the tuple shape that goes into Firebird INSERTs.
No DB connection required — covers the data contract verified against
the production schema (CAB_VENDAS / CORPO_VENDAS).
"""
from __future__ import annotations

from datetime import date

from app.erp.mapper import FireSistemasMapper
from app.models.order import ERPRow, Order, OrderHeader


def test_order_to_cabvendas_uses_status_pedido_and_retailer_ref() -> None:
    order = Order(
        header=OrderHeader(
            order_number="AW097",
            issue_date="15/04/2026",
            customer_name="X",
            customer_cnpj="12345678000190",
        ),
        items=[],
    )

    row = FireSistemasMapper().order_to_cabvendas(order, header_pk=42, client_id=7)

    assert row[0] == 42  # CODIGO
    assert row[2] == date(2026, 4, 15)  # DATA_PEDIDO
    assert row[3] == 7  # CLIENTE FK
    assert row[4] == "PEDIDO", "STATUS must be 'PEDIDO' per production convention"
    assert row[5] == "AW097", "PEDIDO_CLIENTE carries the retailer's reference"
    assert row[6] is None  # OBS
    assert row[7] is None  # DT_ENTREGA on header (item-level only)
    assert row[8] == "IMPORTADOR"  # ULT_INS_USER


def test_order_to_cabvendas_falls_back_to_today_when_no_date() -> None:
    order = Order(
        header=OrderHeader(order_number="X", customer_name="X", customer_cnpj="1"),
        items=[],
    )
    row = FireSistemasMapper().order_to_cabvendas(order, header_pk=1, client_id=1)
    assert row[2] == date.today()


def test_item_to_corpovendas_computes_total_when_missing() -> None:
    item = ERPRow(
        pedido="P1",
        descricao="TENIS X",
        quantidade=4,
        preco_unitario=25.0,
        valor_total=None,  # force computation
    )

    row = FireSistemasMapper().item_to_corpovendas(
        item=item, item_pk=10, header_pk=42, product_seq=999,
    )

    assert row[0] == 10
    assert row[1] == 42
    assert row[2] == 999
    assert row[3] == "TENIS X"
    assert row[4] == 4
    assert row[5] == 25.0
    assert row[6] == 100.0, "total = qty * unit when not provided"
    assert row[7] == "UN"


def test_item_to_corpovendas_truncates_description_to_100_chars() -> None:
    long_desc = "A" * 200
    item = ERPRow(pedido="P1", descricao=long_desc, quantidade=1)
    row = FireSistemasMapper().item_to_corpovendas(item, item_pk=1, header_pk=1, product_seq=None)
    assert len(row[3]) == 100
    assert row[2] is None  # CODPRODUTO may be NULL
