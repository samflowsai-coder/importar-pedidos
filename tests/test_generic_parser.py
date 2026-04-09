from app.parsers.generic_parser import GenericParser


def test_parses_order_from_table():
    parser = GenericParser()
    extracted = {
        "text": "Pedido: PD-123\nCliente: Empresa ABC\nData: 08/04/2026",
        "tables": [
            [["Produto A", "10"], ["Produto B", "5.5"]],
        ],
    }
    order = parser.parse(extracted)
    assert order is not None
    assert order.header.order_number == "PD-123"
    assert len(order.items) == 2
    assert order.items[0].description == "Produto A"
    assert order.items[0].quantity == 10.0


def test_returns_none_when_no_items():
    parser = GenericParser()
    extracted = {"text": "Pedido: PD-999", "tables": []}
    order = parser.parse(extracted)
    assert order is None


def test_parses_header_fields():
    parser = GenericParser()
    extracted = {
        "text": "Pedido: ORD-456\nCliente: Fornecedor XYZ\nData: 01/01/2026",
        "tables": [[["Item 1", "3"]]],
    }
    order = parser.parse(extracted)
    assert order.header.order_number == "ORD-456"
    assert order.header.customer_name == "Fornecedor XYZ"
    assert order.header.issue_date == "01/01/2026"
