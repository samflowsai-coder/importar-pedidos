"""Testes da query em lote de reconciliação (app/erp/queries.py)."""

import pytest

from app.erp.queries import FIND_ORDERS_BY_PEDIDO_CLIENTE


def test_gera_um_placeholder_por_numero():
    sql = FIND_ORDERS_BY_PEDIDO_CLIENTE(3)
    assert sql.count("?") == 3
    assert "IN (?, ?, ?)" in sql


def test_lista_vazia_levanta_value_error():
    """IN () é SQL inválida no Firebird — falhar aqui, não no banco do cliente."""
    with pytest.raises(ValueError):
        FIND_ORDERS_BY_PEDIDO_CLIENTE(0)
