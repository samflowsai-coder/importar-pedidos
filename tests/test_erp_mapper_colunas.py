# tests/test_erp_mapper_colunas.py
from __future__ import annotations

import re
from datetime import date
from decimal import Decimal

from app.erp import queries
from app.erp.fiscal import perfil_para
from app.erp.mapper import FireSistemasMapper
from app.models.order import Order, OrderHeader, OrderItem

# Ordem posicional de INSERT_CAB_VENDAS apos esta task.
CAB = [
    "CODIGO",
    "CODEMPRESA",
    "DATA_PEDIDO",
    "CLIENTE",
    "STATUS",
    "PEDIDO_CLIENTE",
    "OBS",
    "DT_ENTREGA",
    "DT_BASE_FAT",
    "VALOR_TOTAL",
    "TOTAL_PRODUTO",
    "DESCONTO",
    "TIPO_COB",
    "COD_CLASS_FINAN",
    "DESC_CLASS_FINAN",
    "CLASSIF_FAT",
    "CODFIGFISCAL",
    "MECANICO",
    "SEM_IMP",
    "PED_ZF",
    "EH_VENDACONSUMIDOR",
    "VENDEDOR_COMI",
    "ULT_INS_USER",
]


def _pedido():
    return Order(
        header=OrderHeader(
            order_number="OC-70610",
            issue_date="24/08/2026",
            customer_name="DAJU LTDA",
            customer_cnpj="76.917.624/0004-82",
        ),
        items=[OrderItem(description="KIT", quantity=300.0, unit_price=16.12)],
    )


def _campos(t):
    assert len(t) == len(CAB), f"esperado {len(CAB)} colunas, veio {len(t)}"
    return dict(zip(CAB, t))


def test_cabvendas_preenche_colunas_de_100_porcento():
    """As colunas presentes em 100% dos pedidos digitados nao podem sair NULL."""
    t = FireSistemasMapper().order_to_cabvendas(
        _pedido(),
        header_pk=4676,
        client_id=2,
        perfil=perfil_para(None),
        valor_total=Decimal("71899.08"),
    )
    c = _campos(t)
    assert c["VALOR_TOTAL"] == Decimal("71899.08")
    assert c["TOTAL_PRODUTO"] == Decimal("71899.08")
    assert c["DESCONTO"] == Decimal("0")
    assert c["TIPO_COB"] == 4
    assert c["COD_CLASS_FINAN"] == 335
    assert c["DESC_CLASS_FINAN"] == "Venda de Produtos"
    assert c["SEM_IMP"] == "Nao"
    assert c["PED_ZF"] == "Nao"
    assert c["EH_VENDACONSUMIDOR"] == "Nao"
    assert c["VENDEDOR_COMI"] == Decimal("0")
    assert c["MECANICO"] == 99
    assert c["CLASSIF_FAT"] == 1
    assert c["ULT_INS_USER"] == "IMPORTADOR"


def test_cabvendas_usa_codfigfiscal_do_perfil():
    """Nasmar (5) e MM (1) escrevem figura fiscal diferente."""
    t = FireSistemasMapper().order_to_cabvendas(
        _pedido(),
        header_pk=1,
        client_id=2,
        perfil=perfil_para({"fiscal_codfigfiscal": 5}),
        valor_total=Decimal("10"),
    )
    assert _campos(t)["CODFIGFISCAL"] == 5


def test_cabvendas_grava_obs_e_dt_entrega():
    """Hoje mapper.py:74-75 crava None nos dois. O lote depende dos dois."""
    t = FireSistemasMapper().order_to_cabvendas(
        _pedido(),
        header_pk=1,
        client_id=2,
        perfil=perfil_para(None),
        valor_total=Decimal("10"),
        obs="LOTE NAS-2026-S34 | PEDIDOS .4: 1157, 1158",
        dt_entrega=date(2026, 10, 8),
    )
    c = _campos(t)
    assert c["OBS"] == "LOTE NAS-2026-S34 | PEDIDOS .4: 1157, 1158"
    assert c["DT_ENTREGA"] == date(2026, 10, 8)
    assert c["DT_BASE_FAT"] == date(2026, 10, 8)


def test_cabvendas_codped_pai_nao_esta_no_insert():
    """CODPED_PAI e auto-referencia (=CODIGO). Vai por UPDATE pos-insert,
    nao pela tupla — senao o valor teria que ser conhecido antes do PK."""
    assert "CODPED_PAI" not in CAB


def _colunas_do_insert_cab_vendas() -> list[str]:
    """Extrai a lista de colunas de INSERT_CAB_VENDAS do SQL de verdade —
    nao de uma transcricao a mao. Se o SQL ganhar/perder uma coluna sem CAB
    acompanhar, este teste quebra em vez dos quatro de cima continuarem
    verdes enquanto todo bind depois daquele ponto desloca em silencio.
    """
    match = re.search(
        r"INSERT INTO CAB_VENDAS\s*\((.*?)\)\s*VALUES",
        queries.INSERT_CAB_VENDAS,
        re.DOTALL,
    )
    assert match, "nao encontrei a lista de colunas em INSERT_CAB_VENDAS"
    return [c.strip() for c in match.group(1).split(",")]


def test_cab_bate_com_o_insert_cab_vendas_de_verdade():
    """CAB amarrado ao SQL real, nao a uma copia que pode dessincronizar."""
    cols = _colunas_do_insert_cab_vendas()
    assert CAB == cols[:23]
    # +1 = ULT_ALT_USER, que o exporter duplica fora do mapper (nao entra em CAB).
    assert queries.INSERT_CAB_VENDAS.count("?") == len(CAB) + 1
