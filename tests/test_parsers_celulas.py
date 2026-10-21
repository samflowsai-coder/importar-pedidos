"""Leitura de célula de planilha: nativo e texto têm que dar o MESMO resultado."""

from __future__ import annotations

import datetime as dt

import pytest

from app.parsers import celulas


@pytest.mark.parametrize(
    ("valor", "esperado"),
    [
        (5000, 5000.0),
        (10.94, 10.94),
        ("10,94", 10.94),
        ("1.234,56", 1234.56),
        ("1.300", 1300.0),  # 3 dígitos depois do ponto = milhar, nunca 1.3
        ("300.0", 300.0),
        ("R$ 54.700,00", 54700.0),
        ("1,234.56", None),  # formato US: ambíguo, recusa em vez de errar por 1000
        ("", None),
        ("-", None),
        ("abc", None),
        (None, None),
        (True, None),
    ],
)
def test_numero(valor, esperado):
    assert celulas.numero(valor) == esperado


@pytest.mark.parametrize(
    ("valor", "esperado"),
    [
        (7909607842231.0, "7909607842231"),  # float inteiro do openpyxl, sem '.0'
        (7909607842231, "7909607842231"),
        ("  UY44542298 ", "UY44542298"),
        (dt.datetime(2026, 8, 12), "12/08/2026"),
        ("", None),
        (None, None),
    ],
)
def test_texto(valor, esperado):
    assert celulas.texto(valor) == esperado


@pytest.mark.parametrize(
    ("valor", "esperado"),
    [
        (dt.datetime(2026, 10, 21), "21/10/2026"),
        (dt.date(2026, 10, 21), "21/10/2026"),
        (46316, "21/10/2026"),  # serial Excel
        ("21/10/26", "21/10/26"),
        (5000, "5000"),  # fora da faixa de serial: não vira data
        (None, None),
        (True, None),
    ],
)
def test_data(valor, esperado):
    assert celulas.data(valor) == esperado
