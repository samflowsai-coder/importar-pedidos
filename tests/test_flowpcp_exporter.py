from __future__ import annotations

from unittest.mock import patch

from app.integrations.flowpcp.client import FLOWPCP_TARGET_NAME, RECEBIMENTO_PATH
from app.integrations.flowpcp.exporter import FlowPCPExporter
from app.integrations.flowpcp.mapper import build_recebimento_payload
from app.integrations.flowpcp.schema import RecebimentoRequest
from app.models.order import Order, OrderHeader, OrderItem
from app.persistence import outbox_repo

TENANT = "uuid-mm"


def _order() -> Order:
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


@patch("app.integrations.flowpcp.exporter.outbox_repo.enqueue")
def test_export_enqueues_to_outbox(mock_enqueue):
    sent = FlowPCPExporter(tenant_id=TENANT).enqueue(_order(), import_id="imp-1")
    assert sent is True
    _, kwargs = mock_enqueue.call_args
    assert kwargs["target"] == FLOWPCP_TARGET_NAME
    assert kwargs["endpoint"] == RECEBIMENTO_PATH
    assert kwargs["idempotency_key"] == "send-imp-1"


@patch(
    "app.integrations.flowpcp.exporter.outbox_repo.enqueue",
    side_effect=outbox_repo.OutboxDuplicateError("dup"),
)
def test_export_duplicate_is_noop(mock_enqueue):
    # já enfileirado (re-export) — não é erro
    assert FlowPCPExporter(tenant_id=TENANT).enqueue(_order(), import_id="imp-1") is True
