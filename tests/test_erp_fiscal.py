from __future__ import annotations

from decimal import Decimal

from app.erp.fiscal import perfil_para


def test_perfil_default_usa_valores_da_mm():
    """Sem env, cai no perfil da MM Americanense (medido em producao)."""
    p = perfil_para(None)
    assert p.codfigfiscal == 1
    assert p.tipo_cob == 4
    assert p.cod_class_finan == 335
    assert p.desc_class_finan == "Venda de Produtos"
    assert p.classif_fat == 1
    assert p.mecanico == 99
    assert p.icms_porc == Decimal("18")
    assert p.reducao == Decimal("61.11")
    assert p.cfop_principal == "5.101"


def test_perfil_le_codfigfiscal_do_ambiente():
    """A Nasmar usa figura fiscal 5; a MM usa 1. E a unica que difere."""
    p = perfil_para({"fiscal_codfigfiscal": 5})
    assert p.codfigfiscal == 5
    assert p.tipo_cob == 4  # os demais seguem o default


def test_perfil_ignora_campo_vazio():
    """Coluna NULL no ambiente nao zera o default — cai no valor medido."""
    p = perfil_para({"fiscal_codfigfiscal": None, "fiscal_mecanico": None})
    assert p.codfigfiscal == 1
    assert p.mecanico == 99


def test_perfil_e_imutavel():
    p = perfil_para(None)
    with __import__("pytest").raises(Exception):
        p.codfigfiscal = 9
