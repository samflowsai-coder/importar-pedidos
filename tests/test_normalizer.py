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
