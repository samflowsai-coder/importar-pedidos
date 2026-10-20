"""Escopo do vínculo de produto: de quem é o item, em chaves `rede:<id>` / `raiz:<8>`.

O vínculo (chave do cliente -> produto do Fire) vale para a rede do cliente e,
na falta de rede, para a raiz do CNPJ. Aqui só se resolve o escopo; quem
descobre o cliente real de um pedido intercompany é o check (recebe
`cnpj_real` já resolvido).
"""
from __future__ import annotations

from typing import TYPE_CHECKING

from app.erp.preco_regra import normalizar_cnpj
from app.persistence import precos_repo

if TYPE_CHECKING:
    import sqlite3

    from app.models.order import Order


def cnpj_do_item(order: Order, indice: int, *, cnpj_real: str | None = None) -> str | None:
    """CNPJ que define o escopo do item (14 caracteres, formato do contrato de preço).

    Ordem: cliente real do intercompany, cabeçalho do pedido, CNPJ de entrega
    do item. Normaliza com `normalizar_cnpj` (aceita CNPJ alfanumérico, igual à
    tabela de preço); candidato fora do formato é pulado, nunca truncado.
    """
    candidatos: list[str | None] = [cnpj_real, order.header.customer_cnpj]
    if 0 <= indice < len(order.items):
        candidatos.append(order.items[indice].delivery_cnpj)
    for candidato in candidatos:
        cnpj = normalizar_cnpj(candidato)
        if cnpj:
            return cnpj
    return None


def escopos_para(conn: sqlite3.Connection, cnpj: str | None) -> list[str]:
    """Escopos de busca do vínculo, do mais específico ao menos: rede, depois raiz."""
    cnpj = normalizar_cnpj(cnpj)
    if cnpj is None:
        return []
    escopos: list[str] = []
    rede = precos_repo.resolver_rede(conn, cnpj)
    if rede is not None:
        escopos.append(f"rede:{rede.id}")
    escopos.append(f"raiz:{cnpj[:8]}")
    return escopos


def escopo_de_gravacao(conn: sqlite3.Connection, cnpj: str | None) -> str | None:
    """Escopo em que um vínculo novo é gravado: o mais específico disponível."""
    escopos = escopos_para(conn, cnpj)
    return escopos[0] if escopos else None
