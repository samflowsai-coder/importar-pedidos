from __future__ import annotations

from unittest.mock import MagicMock

from app.integrations.flowpcp.exporter import FlowPCPExporter
from app.integrations.flowpcp.mapper import build_recebimento_payload
from app.integrations.flowpcp.schema import RecebimentoRequest
from app.models.order import Order, OrderHeader, OrderItem

TENANT = "uuid-mm"


def _order():
    return Order(
        header=OrderHeader(
            order_number="AW097",
            issue_date="15/06/2026",
            customer_name="MM",
            customer_cnpj="12345678000190",
        ),
        items=[
            OrderItem(
                description="meia preta",
                product_code="ABC",
                ean="789",
                quantity=10,
                unit_price=12.5,
                delivery_date="22/06/2026",
            )
        ],
    )


def test_mapper_shape():
    req = build_recebimento_payload(import_id="imp-1", order=_order(), tenant_id=TENANT)
    assert isinstance(req, RecebimentoRequest)
    assert req.externalId == "imp-1"
    assert req.pedidoNumero == "AW097"
    assert req.cliente.cnpj == "12345678000190"
    assert len(req.itens) == 1
    assert req.itens[0].descricao == "meia preta"
    assert req.itens[0].quantidade == 10


def test_export_sends_when_ok():
    client = MagicMock()
    sent = FlowPCPExporter(client, tenant_id=TENANT).export(_order(), import_id="imp-1")
    assert sent is True
    client.send_order.assert_called_once()


def test_export_enqueues_on_failure(monkeypatch):
    client = MagicMock()
    client.send_order.side_effect = RuntimeError("rede caiu")
    enq = MagicMock()
    monkeypatch.setattr("app.integrations.flowpcp.exporter.outbox_repo.enqueue", enq)
    sent = FlowPCPExporter(client, tenant_id=TENANT).export(_order(), import_id="imp-1")
    assert sent is False
    enq.assert_called_once()
