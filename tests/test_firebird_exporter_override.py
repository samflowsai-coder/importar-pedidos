"""Unit tests for the manual cliente-override path on FirebirdExporter.

Mocks the Firebird cursor — does NOT touch a real .fdb. We assert that:
  - When `override_client_id` is provided, the exporter validates it via
    FIND_CLIENT_BY_CODIGO and skips the CNPJ lookup.
  - When the override codigo no longer exists in CADASTRO, the exporter
    surfaces a clean CLIENT_NOT_FOUND (not a downstream FK error).
  - The legacy CNPJ path is unaffected when no override is passed.
"""
from __future__ import annotations

from unittest.mock import MagicMock

from app.erp import queries
from app.exporters.firebird_exporter import FirebirdExporter
from app.models.order import Order, OrderHeader, OrderItem


def _order() -> Order:
    return Order(
        header=OrderHeader(
            order_number="OVR-1",
            customer_name="ACME LTDA",
            customer_cnpj="11.222.333/0001-44",
        ),
        items=[OrderItem(description="ITEM A", quantity=2, ean="7891234567890")],
    )


def _exporter_with_fake_cursor(monkeypatch, cursor: MagicMock) -> FirebirdExporter:
    """Build an exporter wired to a fake cursor — bypasses connect() entirely."""
    exporter = FirebirdExporter()

    fake_conn = MagicMock()
    fake_conn.cursor.return_value = cursor

    class _CtxConn:
        def __enter__(self):  # noqa: N805 — context manager protocol
            return fake_conn

        def __exit__(self, *a):  # noqa: N805
            return False

    monkeypatch.setattr(exporter, "_conn", MagicMock())
    exporter._conn.is_configured.return_value = True
    exporter._conn.connect.return_value = _CtxConn()
    return exporter


def test_validate_client_id_returns_codigo_when_active(monkeypatch):
    cur = MagicMock()
    cur.fetchone.return_value = (4242, "ACME LTDA", "11222333000144")
    exporter = FirebirdExporter()
    result = exporter._validate_client_id(cur, 4242)
    assert result == 4242
    cur.execute.assert_called_once_with(queries.FIND_CLIENT_BY_CODIGO, (4242,))


def test_validate_client_id_returns_none_when_inactive(monkeypatch):
    cur = MagicMock()
    cur.fetchone.return_value = None
    exporter = FirebirdExporter()
    assert exporter._validate_client_id(cur, 99999) is None


def test_export_uses_override_when_provided(monkeypatch):
    """Override path skips FIND_CLIENT_BY_CNPJ; full insert proceeds happily."""
    cur = MagicMock()
    # Sequencia esperada de chamadas SQL durante _insert_order com override:
    #   1) FIND_CLIENT_BY_CODIGO  → (4242, ...)
    #   2) CHECK_ORDER_EXISTS     → (0,)  (não duplicado)
    #   3) GET_NEXT_CABVENDAS_CODIGO → (100,)
    #   4) INSERT_CAB_VENDAS      → ()
    #   5) por item: FIND_PRODUCT_BY_EAN, GET_NEXT_CORPOVENDAS_CODIGO, INSERT_CORPO_VENDAS
    cur.fetchone.side_effect = [
        (4242, "ACME LTDA", "11222333000144"),  # FIND_CLIENT_BY_CODIGO
        (0,),                                    # CHECK_ORDER_EXISTS
        (100,),                                  # GET_NEXT_CABVENDAS_CODIGO
        (777, "TENIS A", 99.9),                  # FIND_PRODUCT_BY_EAN
        (200,),                                  # GET_NEXT_CORPOVENDAS_CODIGO
    ]

    exporter = _exporter_with_fake_cursor(monkeypatch, cur)
    result = exporter.export(_order(), override_client_id=4242)

    assert result.skipped is False
    assert result.fire_codigo == 100
    assert result.items_inserted == 1

    # Crítico: NÃO chamou FIND_CLIENT_BY_CNPJ — usou apenas o override.
    sql_executed = [c.args[0] for c in cur.execute.call_args_list]
    assert queries.FIND_CLIENT_BY_CODIGO in sql_executed
    assert queries.FIND_CLIENT_BY_CNPJ not in sql_executed


def test_export_override_invalid_raises_client_not_found(monkeypatch):
    """Override codigo inativado/removido → CLIENT_NOT_FOUND, não erro de FK."""
    cur = MagicMock()
    cur.fetchone.return_value = None  # FIND_CLIENT_BY_CODIGO retorna nada

    exporter = _exporter_with_fake_cursor(monkeypatch, cur)
    result = exporter.export(_order(), override_client_id=99999)

    # FirebirdClientNotFoundError é capturado em export() e vira skip_reason.
    assert result.skipped is True
    assert result.skip_reason == "CLIENT_NOT_FOUND"


def test_export_no_override_uses_cnpj_lookup(monkeypatch):
    """Caminho clássico (sem override): segue resolvendo cliente por CNPJ."""
    cur = MagicMock()
    cur.fetchone.side_effect = [
        (4242, "ACME LTDA"),               # FIND_CLIENT_BY_CNPJ
        (0,),                               # CHECK_ORDER_EXISTS
        (100,),                             # GET_NEXT_CABVENDAS_CODIGO
        (777, "TENIS A", 99.9),             # FIND_PRODUCT_BY_EAN
        (200,),                             # GET_NEXT_CORPOVENDAS_CODIGO
    ]

    exporter = _exporter_with_fake_cursor(monkeypatch, cur)
    result = exporter.export(_order())  # sem kwarg

    assert result.skipped is False
    assert result.fire_codigo == 100

    sql_executed = [c.args[0] for c in cur.execute.call_args_list]
    assert queries.FIND_CLIENT_BY_CNPJ in sql_executed
    assert queries.FIND_CLIENT_BY_CODIGO not in sql_executed


def test_export_skipped_when_fb_not_configured(monkeypatch):
    """Sanity: contrato existente preservado quando FB_DATABASE não está setado."""
    exporter = FirebirdExporter()
    monkeypatch.setattr(exporter, "_conn", MagicMock())
    exporter._conn.is_configured.return_value = False

    result = exporter.export(_order(), override_client_id=4242)
    assert result.skipped is True
    assert result.skip_reason == "FB_DATABASE_NOT_SET"
