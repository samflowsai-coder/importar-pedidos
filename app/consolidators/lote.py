"""Consolida N pedidos de origem em 1 pedido espelho do lote.

Puro: sem Firebird, sem SQLite, sem HTTP. Recebe `Order` e devolve `Order`
mais a composicao (de quais itens de origem cada linha saiu).

Dois fatos medidos na Fire viva:

1. Itens sao SOMADOS por produto: 168 linhas viraram 65 no lote #4619, com a
   quantidade preservada exata (1.739 -> 1.739).

2. O preco entra na chave de agrupamento. O mesmo produto aparece com precos
   diferentes na mesma janela (Nacional Lojas: R$ 32,00 num CD e R$ 36,00 em
   outros). Fundir obrigaria a escolher um preco, e escolher errado e base de
   calculo errada na nota fiscal.

Tudo em Decimal: `OrderItem.quantity`/`unit_price` sao float, e somar floats
(0.1 + 0.2) deixa ruido que vira diferenca de centavo no total da nota.
Converte na borda com `Decimal(str(x))` e quantiza antes de devolver.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import ROUND_HALF_UP, Decimal

from app.models.order import Order, OrderHeader, OrderItem

_QUANT = Decimal("0.0001")  # 4 casas, como o Fire guarda
MODOS_PRECO = ("desconto", "divisor")


@dataclass(frozen=True)
class PernaDoLote:
    import_id: str
    order: Order
    fire_codigo_origem: int


@dataclass(frozen=True)
class Origem:
    """Um item de origem que alimentou uma linha do espelho."""

    import_id: str
    indice: int  # posicao do item em `order.items` da perna
    quantidade: Decimal


@dataclass(frozen=True)
class Consolidado:
    order: Order
    composicao: list[list[Origem]]  # composicao[i] = origens de order.items[i]
    obs: str


def preco_espelho(preco: Decimal, *, fator: Decimal, modo: str) -> Decimal:
    """Preco da perna espelho, quantizado em 4 casas.

    'desconto' -> preco * (1 - fator). Leitura literal de "nota menos 7%".
    'divisor'  -> preco / (1 + fator). O que esta gravado no Fire historico:
                  16,12 na Nasmar virou 15,0654205607477 na MM.

    Sao contas diferentes (~0,46% do valor); a escolha e comercial. O modo e
    explicito, sem default aqui.
    """
    if not fator.is_finite() or not (Decimal("0") <= fator < Decimal("1")):
        raise ValueError(f"fator invalido: {fator!r} (use 0 <= fator < 1)")
    if modo == "desconto":
        return (preco * (Decimal("1") - fator)).quantize(_QUANT, rounding=ROUND_HALF_UP)
    if modo == "divisor":
        return (preco / (Decimal("1") + fator)).quantize(_QUANT, rounding=ROUND_HALF_UP)
    raise ValueError(f"modo_preco invalido: {modo!r} (use {MODOS_PRECO})")


def consolidar(
    pernas: list[PernaDoLote],
    *,
    env_origem_slug: str,
    cnpj_revenda: str,
    nome_revenda: str,
    chave_lote: str,
    fator_preco: Decimal,
    modo_preco: str,
) -> Consolidado:
    """Um pedido no nome da revenda com os itens de todas as pernas."""
    if not pernas:
        raise ValueError("lote sem pernas: nao ha o que consolidar")
    if modo_preco not in MODOS_PRECO:
        raise ValueError(f"modo_preco invalido: {modo_preco!r} (use {MODOS_PRECO})")

    # chave: (produto, ean, descricao, data de entrega, preco JA convertido)
    chaves: dict[tuple, int] = {}
    linhas: list[dict] = []
    composicao: list[list[Origem]] = []

    for perna in sorted(pernas, key=lambda p: p.fire_codigo_origem):
        for indice, item in enumerate(perna.order.items):
            onde = f"import {perna.import_id}, item {indice}"
            if item.unit_price is None:
                raise ValueError(f"item sem preço ({onde})")
            bruto = Decimal(str(item.unit_price))
            if not bruto.is_finite() or bruto <= 0:
                raise ValueError(f"item com preço inválido: {item.unit_price!r} ({onde})")
            qtd = Decimal(str(item.quantity)) if item.quantity is not None else Decimal("0")
            if not qtd.is_finite() or qtd <= 0:
                raise ValueError(f"item com quantidade inválida (<= 0 ou não finita) ({onde})")
            preco = preco_espelho(bruto, fator=fator_preco, modo=modo_preco)
            chave = (item.product_code, item.ean, item.description, item.delivery_date, preco)
            pos = chaves.get(chave)
            if pos is None:
                pos = len(linhas)
                chaves[chave] = pos
                linhas.append(
                    {"item": item, "preco": preco, "qtd": Decimal("0")},
                )
                composicao.append([])
            linhas[pos]["qtd"] += qtd
            composicao[pos].append(Origem(perna.import_id, indice, qtd))

    itens = [
        OrderItem(
            description=ln["item"].description,
            product_code=ln["item"].product_code,
            ean=ln["item"].ean,
            quantity=float(ln["qtd"]),
            unit_price=float(ln["preco"]),
            total_price=float((ln["preco"] * ln["qtd"]).quantize(_QUANT, rounding=ROUND_HALF_UP)),
            delivery_date=ln["item"].delivery_date,
        )
        for ln in linhas
    ]

    codigos = sorted({p.fire_codigo_origem for p in pernas})
    obs = f"LOTE {chave_lote} | PEDIDOS {env_origem_slug.upper()}: " + ", ".join(
        str(c) for c in codigos
    )

    order = Order(
        header=OrderHeader(
            order_number=chave_lote,
            issue_date=date.today().strftime("%d/%m/%Y"),
            customer_name=nome_revenda,
            customer_cnpj=cnpj_revenda,
        ),
        items=itens,
        source_file=f"lote:{chave_lote}",
    )
    return Consolidado(order=order, composicao=composicao, obs=obs)
