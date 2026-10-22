"""Vínculo forte de produto no `/recebimento`: o SEQ do Fire por item (`erpProdutoId`).

O Flow resolve `produtoCodigo` como `produtos.codigo`, depois como código do cliente, depois
EAN. Quando o Portal manda o código do varejista, o Flow não acha o produto mesmo com o
Portal já tendo decidido qual é (182 itens sem produto em 22/10). `erpProdutoId` é ADICIONAL:
o que resolve hoje continua igual.
"""

from __future__ import annotations

from app.models.order import Order


def _seq_valido(valor) -> str | None:
    try:
        n = int(valor)
    except (TypeError, ValueError):
        return None
    return str(n) if n > 0 else None


def seqs_por_item(
    n_itens: int,
    *,
    produtos_por_item: dict[int, int] | None = None,
    check: dict | None = None,
) -> dict[int, str]:
    """{posição em order.items: SEQ do Fire}. Do mais fraco ao mais forte: o produto que o
    check casou; a decisão de produto do check (modo `ligado`); o mapa explícito do envio
    (de-para / escolha pontual). Check de outro tamanho que o pedido não vale."""
    saida: dict[int, str] = {}
    itens = check.get("items") if isinstance(check, dict) else None
    if isinstance(itens, list) and len(itens) == n_itens:
        for idx, item in enumerate(itens):
            if not isinstance(item, dict):
                continue
            produto = item.get("produto") if isinstance(item.get("produto"), dict) else {}
            seq = _seq_valido(produto.get("fire_seq")) if produto.get("estado") == "ok" else None
            if seq is None and item.get("match"):
                seq = _seq_valido(item.get("fire_product_id"))
            if seq is not None:
                saida[idx] = seq
    for idx, valor in (produtos_por_item or {}).items():
        seq = _seq_valido(valor)
        if seq is not None and 0 <= idx < n_itens:
            saida[idx] = seq
    return saida


def anexar_produto_erp(order: Order, seqs: dict[int, str]) -> None:
    """Muta `order.items[i].erp_produto_id` (só o envio ao Flow lê)."""
    for idx, seq in seqs.items():
        if 0 <= idx < len(order.items):
            order.items[idx].erp_produto_id = seq
