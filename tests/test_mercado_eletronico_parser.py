"""Mercado Eletrônico (Riachuelo): quantidade com ponto de milhar.

Incidente de 30/10/2026: `2.795,0000ST` virava 795 e `1.073,0000ST` virava 73.
"""

import re
from pathlib import Path

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


# ---------------------------------------------------------------------------
# Item CANCELADO (chamado Bianca/Camila 05/11/2026, PO 6702663768, Fire 5272)
#
# Pedido alterado pela Riachuelo volta com o item marcado "CANCELADO" sob o
# número do item; o ME mantém quantidade e preço unitário e só zera o
# "Preço Total Item". O portal importava o item como se valesse.
# ---------------------------------------------------------------------------

_SAMPLE = Path(__file__).parent.parent / "samples" / "PEDIDO 6702604130.pdf"
_precisa_sample = pytest.mark.skipif(not _SAMPLE.exists(), reason="sample ausente")


def _texto_sample() -> str:
    import pdfplumber

    with pdfplumber.open(_SAMPLE) as pdf:
        return "\n".join(p.extract_text() or "" for p in pdf.pages)


def _bloco(texto: str, numero: str) -> re.Match:
    m = re.search(
        rf"^{numero}\. .*?Descri[çc][aã]o do Material:\n(\d+)_.*?CNPJ:\s*([\d./-]{{18}})"
        r".*?Pre[çc]o Total Item:\s*([\d.,]+)",
        texto,
        re.M | re.S,
    )
    assert m, f"item {numero} não está no sample"
    return m


def _cancelar(texto: str, numero: str, *, marca: str | None = "abaixo") -> str:
    """Faz o item `numero` sair como o ME imprime um cancelado.

    Zera o Preço Total Item e põe "CANCELADO" em `marca`: "abaixo" (linha do
    "já incluso", como na foto), "na_linha" (logo após o número), "acima" (antes
    da linha do número, caindo no bloco do item anterior) ou None (sem a palavra).
    """
    m = _bloco(texto, numero)
    texto = texto[: m.start(3)] + "0,00" + texto[m.end(3) :]
    linha = re.search(rf"^{numero}\. .*\n", texto, re.M)
    if marca == "abaixo":
        assert texto[linha.end() :].startswith("já incluso"), "layout do sample mudou"
        return texto[: linha.end()] + "CANCELADO " + texto[linha.end() :]
    if marca == "na_linha":
        return (
            texto[: linha.start()]
            + linha.group(0).replace(f"{numero}. ", f"{numero}. CANCELADO ", 1)
            + texto[linha.end() :]
        )
    if marca == "acima":
        return texto[: linha.start()] + "CANCELADO\n" + texto[linha.start() :]
    return texto


def _chave(item) -> tuple:
    return (item.product_code, item.quantity, item.delivery_cnpj)


@_precisa_sample
@pytest.mark.parametrize("marca", ["abaixo", "na_linha", "acima", None])
def test_item_cancelado_fica_fora_do_pedido(marca):
    texto = _texto_sample()
    original = MercadoEletronicoParser().parse({"text": texto})
    codigo, cnpj, _ = _bloco(texto, "100").groups()
    alvo = [i for i in original.items if (i.product_code, i.delivery_cnpj) == (codigo, cnpj)]
    assert len(alvo) == 1

    order = MercadoEletronicoParser().parse({"text": _cancelar(texto, "100", marca=marca)})

    assert [_chave(i) for i in order.items] == [
        _chave(i) for i in original.items if i is not alvo[0]
    ]


@_precisa_sample
def test_cancelado_avisa_na_obs_so_da_loja_do_item():
    texto = _texto_sample()
    codigo, cnpj, _ = _bloco(texto, "100").groups()
    qtd = MercadoEletronicoParser()._extract_qty(re.search(r"^100\. .*", texto, re.M).group(0))

    order = MercadoEletronicoParser().parse({"text": _cancelar(texto, "100")})

    esperado = (
        f"6702604130 | CANCELADO PELO CLIENTE, FORA DO PEDIDO: ITEM 100 {codigo} QTD {int(qtd)}"
    )
    da_loja = [i for i in order.items if i.delivery_cnpj == cnpj]
    outras = [i for i in order.items if i.delivery_cnpj != cnpj]
    assert da_loja and outras
    assert {i.obs for i in da_loja} == {esperado}
    assert {i.obs for i in outras} == {None}


@_precisa_sample
def test_loja_com_todos_os_itens_cancelados_some_sem_sujar_a_obs_das_outras():
    texto = _texto_sample()
    original = MercadoEletronicoParser().parse({"text": texto})
    _, cnpj, _ = _bloco(texto, "100").groups()
    itens = [m.group(1) for m in re.finditer(r"^(\d+)\. [\d,]+ %", texto, re.M)]
    numeros = [n for n in itens if _bloco(texto, n).group(2) == cnpj]
    assert len(numeros) == 5
    for numero in numeros:
        texto = _cancelar(texto, numero)

    order = MercadoEletronicoParser().parse({"text": texto})

    assert all(i.delivery_cnpj != cnpj for i in order.items)
    assert len(order.items) == len([i for i in original.items if i.delivery_cnpj != cnpj])
    assert {i.obs for i in order.items} == {None}


@_precisa_sample
def test_pedido_sem_cancelado_nao_ganha_obs():
    order = MercadoEletronicoParser().parse({"text": _texto_sample()})
    assert {i.obs for i in order.items} == {None}


@pytest.mark.parametrize(
    "linha_item, linha_status",
    [
        ("200. 0,00 % 7,00 % 156,0000ST BRL 8,97 BRL 1.399,32", "CANCELADO já incluso já incluso"),
        ("200. CANCELADO 0,00 % 7,00 % 156,0000ST BRL 8,97 BRL 1.399,32", "já incluso já incluso"),
    ],
)
def test_pedido_com_todos_os_itens_cancelados_e_recusado(linha_item, linha_status):
    from app.parsers.base_parser import PedidoRecusadoError

    text = (
        "Mercado Eletrônico\n"
        "PEDIDO 6702663768\n"
        f"{linha_item}\n"
        f"{linha_status}\n"
        "Descrição do Material:\n"
        "15968707003_RIAC - KIT MEIA PER 135 00547 K, ST001, 26 A 29\n"
        "Fluxo Recebimento:Caixa Preço Total Item:0,00 Desc.dif. impostos:83,54\n"
    )
    with pytest.raises(PedidoRecusadoError, match=r"pedido 6702663768 estão CANCELADOS.*200"):
        MercadoEletronicoParser().parse({"text": text})


def test_cancelado_na_descricao_do_produto_nao_derruba_o_item():
    text = (
        "Mercado Eletrônico\n"
        "PEDIDO 6702663768\n"
        "50. 0,00 % 7,00 % 15,0000ST BRL 8,97 BRL 134,55\n"
        "já incluso já incluso\n"
        "Descrição do Material:\n"
        "123_RIAC - KIT MEIA CANCELADO ESPECIAL\n"
        "Fluxo Recebimento:Caixa Preço Total Item:113,94 Desc.dif. impostos:20,61\n"
    )
    order = MercadoEletronicoParser().parse({"text": text})
    assert [i.quantity for i in order.items] == [15]
    assert order.items[0].obs is None


def test_item_de_preco_zero_nao_e_cancelado():
    """Bonificação (preço unitário 0) tem Preço Total 0 sem estar cancelada."""
    text = (
        "Mercado Eletrônico\n"
        "PEDIDO 6702663768\n"
        "50. 0,00 % 7,00 % 15,0000ST BRL 0,00 BRL 0,00\n"
        "já incluso já incluso\n"
        "Descrição do Material:\n"
        "123_RIAC - KIT MEIA BRINDE\n"
        "Fluxo Recebimento:Caixa Preço Total Item:0,00 Desc.dif. impostos:0,00\n"
    )
    order = MercadoEletronicoParser().parse({"text": text})
    assert [i.quantity for i in order.items] == [15]


_SAMPLE_CANCELADO = (
    Path(__file__).parent.parent / "samples" / "PEDIDO RIACHUELO 6702663768 CANCELADO.pdf"
)


@pytest.mark.skipif(not _SAMPLE_CANCELADO.exists(), reason="sample ausente")
def test_pdf_real_com_itens_cancelados():
    """PDF do chamado: itens 200 e 300 da LJ190 (Natal) cancelados, Fire 5272."""
    import pdfplumber

    with pdfplumber.open(_SAMPLE_CANCELADO) as pdf:
        texto = "\n".join(p.extract_text() or "" for p in pdf.pages)

    order = MercadoEletronicoParser().parse({"text": texto})

    natal = "33.200.056/0343-96"
    assert len(order.items) == 8
    codigos_natal = sorted(i.product_code for i in order.items if i.delivery_cnpj == natal)
    assert codigos_natal == ["15968707001", "15968707002", "15968707005"]
    assert {i.obs for i in order.items if i.delivery_cnpj == natal} == {
        "6702663768 | CANCELADO PELO CLIENTE, FORA DO PEDIDO: "
        "ITEM 200 15968707003 QTD 156, ITEM 300 15968707004 QTD 166"
    }
    assert {i.obs for i in order.items if i.delivery_cnpj != natal} == {None}
