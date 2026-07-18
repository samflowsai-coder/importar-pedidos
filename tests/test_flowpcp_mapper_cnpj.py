from app.integrations.flowpcp.mapper import build_recebimento_payload
from app.models.order import Order, OrderHeader, OrderItem


def _order(cnpj: str | None) -> Order:
    return Order(
        header=OrderHeader(customer_name="LOJA X", customer_cnpj=cnpj, order_number="123"),
        items=[OrderItem(description="TENIS", quantity=1)],
        source_file="x.pdf",
    )


def test_payload_normalizes_formatted_cnpj():
    req = build_recebimento_payload(
        import_id="imp1", order=_order("06.347.409/0296-51"), tenant_id="t1"
    )
    assert req.cliente.cnpj == "06347409029651"


def test_payload_keeps_none_when_no_cnpj():
    req = build_recebimento_payload(import_id="imp1", order=_order(None), tenant_id="t1")
    assert req.cliente.cnpj is None
