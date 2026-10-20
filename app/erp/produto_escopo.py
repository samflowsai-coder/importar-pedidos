"""Escopo do vínculo de produto: de quem é o item, em chaves `rede:<id>` / `raiz:<8>`.

O vínculo (chave do cliente -> produto do Fire) vale para a rede do cliente e,
na falta de rede, para a raiz do CNPJ. Aqui só se resolve o escopo; quem
descobre o cliente real de um pedido intercompany é o check (recebe
`cnpj_real` já resolvido).
"""
from __future__ import annotations

from typing import TYPE_CHECKING

from app.erp.cnpj import cnpj_digits
from app.persistence import precos_repo

if TYPE_CHECKING:
    import sqlite3

    from app.models.order import Order


def _so_14_digitos(valor: str | None) -> str | None:
    digitos = cnpj_digits(valor)
    return digitos if len(digitos) == 14 else None


def cnpj_do_item(order: Order, indice: int, *, cnpj_real: str | None = None) -> str | None:
    """CNPJ (14 dígitos) que define o escopo do item.

    Ordem: cliente real do intercompany, cabeçalho do pedido, CNPJ de entrega
    do item. Candidato que não tem 14 dígitos é pulado.
    """
    candidatos: list[str | None] = [cnpj_real, order.header.customer_cnpj]
    if 0 <= indice < len(order.items):
        candidatos.append(order.items[indice].delivery_cnpj)
    for candidato in candidatos:
        cnpj = _so_14_digitos(candidato)
        if cnpj:
            return cnpj
    return None


def escopos_para(conn: sqlite3.Connection, cnpj: str | None) -> list[str]:
    """Escopos de busca do vínculo, do mais específico ao menos: rede, depois raiz."""
    cnpj = _so_14_digitos(cnpj)
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
