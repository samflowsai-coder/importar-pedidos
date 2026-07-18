from app.models.order import Order, OrderHeader, OrderItem
from app.normalizers.order_normalizer import OrderNormalizer


def _order(order_number=None, issue_date=None, customer_name=None, items=None):
    return Order(
        header=OrderHeader(order_number=order_number, issue_date=issue_date, customer_name=customer_name),
        items=items or [OrderItem(description="Produto A", quantity=10.0)],
    )


def test_normalizes_order_number_to_uppercase():
    normalizer = OrderNormalizer()
    order = normalizer.normalize(_order(order_number="pd-001"))
    assert order.header.order_number == "PD-001"


def test_normalizes_date_dots_to_slashes():
    normalizer = OrderNormalizer()
    order = normalizer.normalize(_order(issue_date="08.04.2026"))
    assert order.header.issue_date == "08/04/2026"


def test_normalizes_short_year():
    normalizer = OrderNormalizer()
    order = normalizer.normalize(_order(issue_date="08/04/26"))
    assert order.header.issue_date == "08/04/2026"


def test_normalizes_customer_name_title_case():
    normalizer = OrderNormalizer()
    order = normalizer.normalize(_order(customer_name="EMPRESA TESTE LTDA"))
    assert order.header.customer_name == "Empresa Teste Ltda"


def test_normalizes_item_delivery_date_dots_to_slashes():
    # Centauro entrega o prazo em DD.MM.YYYY (pontos). O normalizer tem que
    # canonicalizar como faz com o issue_date — senão os _parse_date/_to_iso
    # dos consumidores (Flow, ERP, gestor), que só aceitam barras, devolvem None.
    normalizer = OrderNormalizer()
    order = normalizer.normalize(
        _order(items=[OrderItem(description="X", quantity=1.0, delivery_date="01.03.2026")])
    )
    assert order.items[0].delivery_date == "01/03/2026"


def test_normalizes_item_delivery_date_short_year():
    normalizer = OrderNormalizer()
    order = normalizer.normalize(
        _order(items=[OrderItem(description="X", quantity=1.0, delivery_date="01/03/26")])
    )
    assert order.items[0].delivery_date == "01/03/2026"


def test_item_without_delivery_date_stays_none():
    normalizer = OrderNormalizer()
    order = normalizer.normalize(
        _order(items=[OrderItem(description="X", quantity=1.0, delivery_date=None)])
    )
    assert order.items[0].delivery_date is None
