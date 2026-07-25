"""Aplica o de-para no Order antes de gerar o XLS.

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


def apply(order: Order, *, conn: sqlite3.Connection) -> list[dict]:
    """Enriquece os itens do `order` com a identidade Fire onde há vínculo
    de-para. Muta `order.items` in place e retorna o resumo do que mudou.

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

    changed: list[dict] = []
    for idx, it in enumerate(order.items):
        dk = None
        if it.product_code:
            dk = ("codigo", _norm_key("codigo", it.product_code))
        if (dk is None or dk not in dm) and it.ean:
            dk = ("ean", _norm_key("ean", it.ean))
        dv = dm.get(dk) if dk else None
        if dv is None:
            continue

        orig = it.product_code or it.ean or ""
        it.product_code = dv["fire_codigo"]
        if dv.get("fire_ean"):
            it.ean = dv["fire_ean"]
        ref_note = f"ref cliente: {orig}"
        it.obs = f"{it.obs} | {ref_note}" if it.obs else ref_note
        changed.append({"item_index": idx, "fire_codigo": dv["fire_codigo"], "orig": orig})
    return changed
