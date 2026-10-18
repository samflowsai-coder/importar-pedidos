"""Avaliador puro da trava de preço: compara cada item do pedido com a tabela
da rede do cliente. Sem I/O — tabela, rede e relógio entram por parâmetro."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Literal

from app.erp.cnpj import cnpj_digits
from app.erp.mapper import parse_date
from app.erp.product_check import _to_cents
from app.models.order import Order
from app.persistence.precos_repo import Rede

Situacao = Literal["ok", "fila", "nao_avaliado", "fora_intercompany"]
Motivo = Literal[
    "igual",
    "acima",
    "abaixo",
    "sem_preco_tabela",
    "sem_preco_pedido",
    "rede_travada",
    "rede_sem_tabela",
    "produto_nao_identificado",
    "cliente_sem_cnpj",
    "copia_desatualizada",
    "empresa_sem_cnpj",
    "intercompany",
]

_LIMITE_COPIA = timedelta(hours=24)


@dataclass(frozen=True)
class ItemAvaliado:
    item_index: int
    fire_produto_id: str | None
    situacao: Situacao
    motivo: Motivo
    preco_pedido_centavos: int | None
    preco_tabela_centavos: int | None


@dataclass(frozen=True)
class Avaliacao:
    rede: Rede | None
    cnpj_rede: str | None
    itens: list[ItemAvaliado] = field(default_factory=list)

    @property
    def pendentes(self) -> list[ItemAvaliado]:
        return [i for i in self.itens if i.situacao == "fila"]

    @property
    def bloqueados(self) -> list[ItemAvaliado]:
        return [i for i in self.itens if i.situacao == "nao_avaliado"]


def cnpj_da_rede(order: Order, cnpj_cliente: str | None) -> str | None:
    """CNPJ que identifica a rede: o do cliente; senão a raiz comum das lojas
    de entrega. Raízes diferentes ou nenhuma → None."""
    proprio = cnpj_digits(cnpj_cliente)
    if len(proprio) in (8, 14):
        return proprio
    raizes = {d[:8] for d in (cnpj_digits(it.delivery_cnpj) for it in order.items) if len(d) >= 8}
    if len(raizes) == 1:
        return next(iter(raizes))
    return None


def _data_referencia(order: Order, agora: datetime) -> date:
    return parse_date(order.header.issue_date) or agora.date()


def avaliar(
    order: Order,
    check: dict,
    *,
    cnpj_cliente: str | None,
    resolver_rede: Callable[[str], Rede | None],
    preco_vigente: Callable[[str, str, date], int | None],
    cnpjs_grupo: set[str],
    ultima_sync: datetime | None,
    agora: datetime,
) -> Avaliacao:
    cnpj_rede = cnpj_da_rede(order, cnpj_cliente)
    check_itens = check.get("items") or []

    def _id(i: int) -> str | None:
        if i >= len(check_itens):
            return None
        bruto = check_itens[i].get("fire_product_id")
        return None if bruto is None else str(bruto)

    def _tudo(situacao: Situacao, motivo: Motivo, rede: Rede | None = None):
        return Avaliacao(
            rede=rede,
            cnpj_rede=cnpj_rede,
            itens=[
                ItemAvaliado(i, _id(i), situacao, motivo, _to_cents(it.unit_price), None)
                for i, it in enumerate(order.items)
            ],
        )

    if cnpj_rede is None:
        return _tudo("nao_avaliado", "cliente_sem_cnpj")
    if cnpj_rede[:8] in cnpjs_grupo:
        return _tudo("fora_intercompany", "intercompany")
    if ultima_sync is None or agora - ultima_sync > _LIMITE_COPIA:
        return _tudo("nao_avaliado", "copia_desatualizada")

    rede = resolver_rede(cnpj_rede)
    data = _data_referencia(order, agora)
    itens: list[ItemAvaliado] = []
    for i, it in enumerate(order.items):
        pid = _id(i)
        pedido = _to_cents(it.unit_price)

        def _item(situacao: Situacao, motivo: Motivo, tabela: int | None = None):
            return ItemAvaliado(i, pid, situacao, motivo, pedido, tabela)

        if pid is None:
            itens.append(_item("nao_avaliado", "produto_nao_identificado"))
        elif rede is None:
            itens.append(_item("fila", "rede_sem_tabela"))
        elif rede.travada:
            itens.append(_item("fila", "rede_travada", preco_vigente(rede.id, pid, data)))
        else:
            tabela = preco_vigente(rede.id, pid, data)
            if not tabela:
                itens.append(_item("fila", "sem_preco_tabela"))
            elif pedido is None:
                itens.append(_item("fila", "sem_preco_pedido", tabela))
            elif pedido == tabela:
                itens.append(_item("ok", "igual", tabela))
            elif pedido > tabela:
                itens.append(_item("fila", "acima", tabela))
            else:
                itens.append(_item("fila", "abaixo", tabela))
    return Avaliacao(rede=rede, cnpj_rede=cnpj_rede, itens=itens)
