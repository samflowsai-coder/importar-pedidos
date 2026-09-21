"""Número do pedido gerado pelo portal quando o documento não traz nenhum.

`order_number` vira `CAB_VENDAS.PEDIDO_CLIENTE`, que o Fire copia para
`NOTAPROD.PEDCLIENTE` ao faturar (o xPed da NF-e, cortado em 15 caracteres —
medido na Fire viva em 21/09/2026: 198/230 notas da MM, 99/100 da Nasmar). Em
modo xlsx é também o ÚNICO campo que liga o pedido do portal à linha do Fire
(o importador de Excel do Fire não leva OBS). Sem número, o pedido fica órfão:
PEDIDO_CLIENTE NULL, sem reconciliação. Com nome de loja no lugar
(`NBA STORE MOGI SHOPP`, pedido 4932), o próximo pedido da mesma loja herda a
mesma "chave".

Regra: o pipeline preenche `SN-` + 8 hex do sha256 dos bytes do arquivo — o
mesmo hash de `imports.file_sha256` — e marca `order_number_gerado`.
"""

from __future__ import annotations

import hashlib
import re
from pathlib import Path

import pytest

from app.erp.numero_pedido import variantes
from app.ingestion.file_loader import LoadedFile
from app.pipeline import process

SAMPLES = Path(__file__).resolve().parent.parent / "samples"
_FORMATO = re.compile(r"SN-[0-9A-F]{8}")


def _load(filename: str) -> LoadedFile:
    path = SAMPLES / filename
    if not path.exists():
        pytest.skip(f"sample missing: {filename}")
    return LoadedFile(path=path, extension=path.suffix.lower(), raw=path.read_bytes())


def test_documento_sem_numero_recebe_numero_do_portal():
    file = _load("Pedido Grupo Afeet Pulmao.xlsx")
    order = process(file)
    assert order is not None
    assert _FORMATO.fullmatch(order.header.order_number)
    assert order.header.order_number_gerado is True


def test_numero_gerado_e_o_prefixo_do_file_sha256():
    """Mesmo hash que `arquivo_recebido.guardar` grava em `imports.file_sha256`:
    do número impresso na nota o suporte chega no import."""
    file = _load("Pedido Grupo Afeet Pulmao.xlsx")
    sha = hashlib.sha256(file.raw).hexdigest()
    assert process(file).header.order_number == f"SN-{sha[:8].upper()}"


def test_mesmo_arquivo_gera_o_mesmo_numero():
    """Reimportar o mesmo arquivo (reimport, preview de novo, worker) não pode
    trocar a chave do pedido no Fire."""
    a = process(_load("Pedido Grupo Afeet Pulmao.xlsx"))
    b = process(_load("Pedido Grupo Afeet Pulmao.xlsx"))
    assert a.header.order_number == b.header.order_number


def test_numero_gerado_cabe_inteiro_no_xped_da_nfe():
    order = process(_load("Pedido Grupo Afeet Pulmao.xlsx"))
    assert len(order.header.order_number) <= 15


def test_numero_do_documento_nunca_e_substituido():
    order = process(_load("Pedido Authentic Fit.xlsx"))
    assert order.header.order_number == "AF198"
    assert order.header.order_number_gerado is False


@pytest.mark.parametrize("numero", ["SN-3F9A12BC", "SN-12345678", "SN-00001234"])
def test_reconciliacao_nao_mutila_o_numero_gerado(numero):
    """O corte de sufixo de loja (`-N` com 1 a 4 dígitos) e o de zeros à
    esquerda não podem transformar o token em outro número — nem quando os 8
    hex saem todos dígitos."""
    assert variantes(numero) == [numero]


@pytest.mark.parametrize("gerado", [True, False])
def test_preview_leva_a_marca_de_numero_gerado(gerado):
    """O selo "Gerado pelo portal" do preview depende deste campo: sem ele a
    operadora vê `SN-...` e confirma como se fosse número do cliente."""
    from app.models.order import Order, OrderHeader, OrderItem
    from app.web.server import _build_preview_payload

    order = Order(
        header=OrderHeader(order_number="SN-3F9A12BC", order_number_gerado=gerado),
        items=[OrderItem(description="KIT", product_code="X", quantity=1)],
    )
    payload = _build_preview_payload("pid", "arquivo.xlsx", order)
    assert payload["header"]["order_number_gerado"] is gerado
