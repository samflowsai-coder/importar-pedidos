"""Aplica o de-para no Order antes de gerar o XLS ou inserir no Fire.

Item resolvido por vínculo sai com a identidade do Fire (codigo/ean), com a
referência original do varejista anexada ao OBS (rastreabilidade). Mutação
transiente: o Order vem de Order.model_validate(snapshot), descartado após o
export. Não toca o snapshot persistido.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from app.persistence import produto_depara_repo
from app.persistence.produto_depara_repo import _norm_key

if TYPE_CHECKING:
    import sqlite3

    from app.models.order import Order


def resolver(order: Order, *, conn: sqlite3.Connection) -> list[dict]:
    """Vínculos de-para dos itens do `order`, SEM mutar nada.

    Uma entrada por item vinculado: `item_index`, `fire_produto_id`,
    `fire_codigo`, `fire_ean`, `orig`. É o mapa que o envio consulta UMA vez
    antes do portão de preço: a trava avalia o mesmo SEQ que o
    `FirebirdExporter` grava e cujo código o XLSX leva (`aplicar`).

    Chave do cliente: `produto_depara_repo.client_key()` — CNPJ (dígitos)
    quando o header tem, senão o nome normalizado (varejistas como
    Riachuelo não trazem CNPJ no header; o CNPJ real é por loja).

    Prioridade de match por item: código primeiro, EAN como fallback —
    mesma ordem do 3º degrau de `product_check.check_order` (mantém os dois
    consumidores do de-para consistentes).
    """
    key = produto_depara_repo.client_key(order.header.customer_cnpj, order.header.customer_name)
    codigos = [it.product_code for it in order.items if it.product_code]
    eans = [it.ean for it in order.items if it.ean]
    dm = produto_depara_repo.lookup(conn, key, codigos=codigos, eans=eans)
    if not dm:
        return []

    vinculos: list[dict] = []
    for idx, it in enumerate(order.items):
        dk = None
        if it.product_code:
            dk = ("codigo", _norm_key("codigo", it.product_code))
        if (dk is None or dk not in dm) and it.ean:
            dk = ("ean", _norm_key("ean", it.ean))
        dv = dm.get(dk) if dk else None
        if dv is None:
            continue
        vinculos.append(
            {
                "item_index": idx,
                "fire_produto_id": dv["fire_produto_id"],
                "fire_codigo": dv["fire_codigo"],
                "fire_ean": dv.get("fire_ean"),
                "orig": it.product_code or it.ean or "",
            }
        )
    return vinculos


def produtos_por_item(vinculos: list[dict]) -> dict[int, int]:
    """{posição em order.items: SEQ} para o `FirebirdExporter` e para o portão."""
    return {v["item_index"]: int(v["fire_produto_id"]) for v in vinculos}


def aplicar(order: Order, vinculos: list[dict]) -> list[dict]:
    """Enriquece os itens do `order` com a identidade Fire dos `vinculos`
    (de `resolver`). Muta `order.items` in place e retorna o resumo do que mudou."""
    changed: list[dict] = []
    for v in vinculos:
        it = order.items[v["item_index"]]
        it.product_code = v["fire_codigo"]
        if v.get("fire_ean"):
            it.ean = v["fire_ean"]
        ref_note = f"ref cliente: {v['orig']}"
        it.obs = f"{it.obs} | {ref_note}" if it.obs else ref_note
        changed.append(
            {
                "item_index": v["item_index"],
                "fire_produto_id": v["fire_produto_id"],
                "fire_codigo": v["fire_codigo"],
                "orig": v["orig"],
            }
        )
    return changed


def apply(order: Order, *, conn: sqlite3.Connection) -> list[dict]:
    """`resolver` + `aplicar` numa chamada (mesmo contrato de antes)."""
    return aplicar(order, resolver(order, conn=conn))
