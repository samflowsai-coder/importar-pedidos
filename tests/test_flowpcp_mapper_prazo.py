"""prazoSolicitado no payload pro Flow — o prazo de entrega tem que sobreviver
ao formato de pontos da Centauro (DD.MM.YYYY).

Regressão: com delivery_date='01.03.2026' (pontos), o _to_iso do mapper fazia
strptime('%d/%m/%Y') → ValueError → prazoSolicitado=None. O Flow então caía no
default emitidoEm e o Gateway via todo pedido como impossível. A raiz é o
OrderNormalizer canonicalizar delivery_date; este teste prova o efeito ponta-a-ponta.
"""
from __future__ import annotations

from app.integrations.flowpcp.mapper import build_recebimento_payload
from app.models.order import Order, OrderHeader, OrderItem
from app.normalizers.order_normalizer import OrderNormalizer


def _order(delivery_date: str | None) -> Order:
    return Order(
        header=OrderHeader(
            order_number="123", issue_date="25/11/2025",
            customer_name="X", customer_cnpj="06347409029651",
        ),
        items=[OrderItem(description="TENIS", quantity=1.0, delivery_date=delivery_date)],
        source_file="x.pdf",
    )


def test_prazo_survives_dot_format_after_normalize():
    order = OrderNormalizer().normalize(_order("01.03.2026"))
    req = build_recebimento_payload(import_id="x", order=order, tenant_id="t")
    assert req.prazoSolicitado == "2026-03-01T00:00:00.000Z"


def test_prazo_none_when_no_delivery_date():
    order = OrderNormalizer().normalize(_order(None))
    req = build_recebimento_payload(import_id="x", order=order, tenant_id="t")
    assert req.prazoSolicitado is None
