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
