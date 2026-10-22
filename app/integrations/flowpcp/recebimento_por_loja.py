"""Pedido sem CNPJ no header (Riachuelo, NBA): um `/recebimento` por loja.

O Flow acha o cliente só pelo CNPJ e o Fire guarda um pedido por loja (CNPJ da filial). O import
combinado vai ao Flow sem CNPJ, e o vínculo com o Fire não enxerga o pedido. Dividido por loja,
cada recebimento leva o CNPJ da filial; todos com o MESMO `externalId` (= import_id) e cada item com
`indiceOriginal` (posição no array combinado): o Flow grava `source_id_externo =
externalId:indiceOriginal`, a chave que o vínculo do SP1 já usa. Atrás de interruptor por ambiente,
desligado: o Flow tem que ler `indiceOriginal` antes (sem isso as lojas seguintes colidem no
índice local e somem em silêncio).
"""

from __future__ import annotations

import os

from app.erp.cnpj import cnpj_digits
from app.erp.depara_cliente import ResolucaoCliente
from app.models.order import Order, OrderItem

ENV_VAR = "PORTAL_FLOWPCP_RECEBIMENTO_POR_LOJA"


def ligado(slug: str) -> bool:
    return slug.lower() in {
        s.strip().lower() for s in os.environ.get(ENV_VAR, "").split(",") if s.strip()
    }


def cnpj_da_loja(item: OrderItem) -> str:
    """CNPJ de 14 dígitos do local de entrega, ou "" (texto livre, CPF, vazio)."""
    d = cnpj_digits(item.delivery_cnpj)
    return d if len(d) == 14 else ""


def deve_dividir(order: Order, resolucao: ResolucaoCliente | None = None) -> bool:
    """Só quando o header NÃO tem CNPJ e TODO item aponta para uma loja de 14 dígitos. Intercompany
    resolvido fica de fora: o cliente real é um só e já vai no `cliente` do envio."""
    if resolucao is not None and resolucao.resolvido:
        return False
    if cnpj_digits(order.header.customer_cnpj):
        return False
    return bool(order.items) and all(cnpj_da_loja(i) for i in order.items)


def lojas(order: Order) -> dict[str, list[int]]:
    """cnpj da loja → posições em `order.items`, na ordem da primeira aparição."""
    saida: dict[str, list[int]] = {}
    for idx, item in enumerate(order.items):
        saida.setdefault(cnpj_da_loja(item), []).append(idx)
    return saida
