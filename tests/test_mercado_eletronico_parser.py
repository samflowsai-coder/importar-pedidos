"""Mercado Eletrônico (Riachuelo): quantidade com ponto de milhar.

Incidente de 30/10/2026: `2.795,0000ST` virava 795 e `1.073,0000ST` virava 73.
"""

import pytest

from app.parsers.mercado_eletronico_parser import MercadoEletronicoParser


def _pdf(first_line: str) -> dict:
    text = (
        "Mercado Eletrônico\n"
        "PEDIDO 6702672674\n"
        f"{first_line}\n"
        "Descrição do Material:\n"
        "123456_MEIA - Meia Social Preta\n"
    )
    return {"text": text}


@pytest.mark.parametrize(
    "line, qty, unit, total",
    [
        ("150. 0,00 % 18,00 % 2.795,0000ST BRL 13,66 BRL 38.179,70", 2795, 13.66, 38179.70),
        ("350. 0,00 % 18,00 % 1.073,0000ST BRL 8,97 BRL 9.624,81", 1073, 8.97, 9624.81),
        ("50. 0,00 % 0,00 % 195,0000ST BRL 13,66 BRL 2.663,70", 195, 13.66, 2663.70),
        ("100. 0,00 % 7,00 % 910,0000 ST BRL 13,66 BRL 12.430,60", 910, 13.66, 12430.60),
        (
            "200. 0,00 % 7,00 % 1.234.567,0000ST BRL 1,00 BRL 1.234.567,00",
            1234567,
            1.00,
            1234567.00,
        ),
        ("250. 0,00 % 7,00 % 2.795ST BRL 13,66 BRL 38.179,70", 2795, 13.66, 38179.70),
        ("300. 0,00 % 7,00 % 1234,0000ST BRL 1,00 BRL 1.234,00", 1234, 1.00, 1234.00),
    ],
)
def test_quantidade_com_milhar_sai_inteira(line, qty, unit, total):
    order = MercadoEletronicoParser().parse(_pdf(line))
    assert order is not None
    item = order.items[0]
    assert item.quantity == qty
    assert item.unit_price == unit
    assert item.total_price == total
