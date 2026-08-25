"""O Fire guarda o número com ruído de digitação manual.

Caso real medido: Sam's Club vive como `06654993-0000` no portal e `06654993`
no Fire. Sem as variantes, todo pedido Sam's é falso negativo silencioso.
"""

import pytest

from app.erp.numero_pedido import variantes


def test_exato_vem_primeiro():
    assert variantes("6702645869")[0] == "6702645869"


def test_sufixo_de_quatro_digitos_vira_variante():
    """Caso Sam's: portal 06654993-0000, Fire 06654993."""
    assert "06654993" in variantes("06654993-0000")


def test_zeros_a_esquerda_viram_variante():
    assert "29852483" in variantes("0029852483")


def test_sem_duplicata_quando_variantes_coincidem():
    assert variantes("6702645869") == ["6702645869"]


def test_espaco_em_volta_e_ignorado():
    assert variantes("  K01  ")[0] == "K01"


@pytest.mark.parametrize("entrada", ["", "   ", None])
def test_entrada_vazia_devolve_lista_vazia(entrada):
    assert variantes(entrada) == []


def test_sufixo_que_nao_e_de_quatro_digitos_nao_e_cortado():
    """`AF-198` não é sufixo de loja; cortar viraria match errado."""
    assert variantes("AF-198") == ["AF-198"]


def test_sufixo_de_um_a_tres_digitos_tambem_vira_variante():
    """Caso real medido na Fire da MM em 2026-08-24: Authentic Feet e Xambre
    mandam `AF049-6` / `AW033-6` e o Fire guarda `AF049` / `AW033`.

    A regra antiga só cortava 4 dígitos (caso Sam's) e deixava 43 dos 137
    pedidos pendentes como falso negativo — 19 de 19 amostrados existiam no
    Fire sob o MESMO CNPJ.
    """
    assert "AF049" in variantes("AF049-6")
    assert "AW033" in variantes("AW033-6")
    assert "AF106" in variantes("AF106 - 96")


def test_espacos_em_volta_do_hifen_do_sufixo_nao_impedem_o_corte():
    """`AF090 - 3` aparece assim no portal e `AF090` no Fire."""
    assert "AF090" in variantes("AF090 - 3")
    assert "AW013" in variantes("AW013 - 3")


def test_corte_exige_que_o_resto_ainda_pareca_numero_de_pedido():
    """A trava que preserva o contraexemplo da spec.

    `AF-198` cortado viraria `AF`, que casaria com qualquer coisa do mesmo
    cliente cujo número fosse `AF`. Só corta quando o resto tem ao menos 3
    caracteres E contém dígito — `AF` falha nos dois.
    """
    assert variantes("AF-198") == ["AF-198"]
    assert "AF" not in variantes("AF-1985")
    assert "K" not in variantes("K-01")
