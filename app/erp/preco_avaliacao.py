"""Avaliador puro da trava de preço: compara cada item do pedido com a tabela
da rede do cliente. Sem I/O — tabela, rede e relógio entram por parâmetro."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Literal

from app.erp.mapper import parse_date
from app.erp.preco_regra import (
    TZ,
    PrecoInvalido,
    PrecoLinha,
    centavos,
    data_referencia,
    normalizar_cnpj,
)
from app.models.order import Order
from app.persistence.precos_repo import Rede

Situacao = Literal["ok", "fila", "nao_avaliado", "fora_intercompany"]
# Nomes do contrato precos v1 (§4.1) + os de bloqueio local do Portal.
Motivo = Literal[
    "ok",
    "rede_travada",
    "pedido_sem_preco",
    "sem_preco_tabela",
    "preco_acima",
    "preco_abaixo",
    "produto_nao_identificado",
    "cliente_sem_cnpj",
    "cliente_sem_rede",
    "unidade_divergente",
    "copia_desatualizada",
    "preco_invalido",
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
    preco_tabela_id: str | None = None
    # Unidade do produto no Fire (strip + maiúsculas); None se o Fire não deu.
    unidade_erp: str | None = None


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
    """CNPJ (contrato §2.3, já normalizado) que resolve a rede: o do cliente;
    senão, se TODAS as lojas de entrega têm CNPJ válido com a mesma raiz, o
    menor deles (ordem de texto). Qualquer outro caso → None."""
    proprio = normalizar_cnpj(cnpj_cliente)
    if proprio is not None:
        return proprio
    lojas = [normalizar_cnpj(it.delivery_cnpj) for it in order.items]
    if not lojas or any(c is None for c in lojas):
        return None
    if len({c[:8] for c in lojas}) != 1:
        return None
    return min(lojas)


def data_ref_do_pedido(order: Order, recebido_em: str | datetime | None, agora: datetime) -> date:
    """Data de referência (§5): a emissão (`issue_date`, já data civil); sem
    ela, a data civil em São Paulo do recebimento (`imported_at`, gravado sem
    fuso na hora local do servidor = São Paulo); sem ele, a de `agora`."""
    emissao = parse_date(order.header.issue_date)
    if emissao is not None:
        return emissao
    recebido: datetime | None = None
    if isinstance(recebido_em, datetime):
        recebido = recebido_em
    elif isinstance(recebido_em, str) and recebido_em.strip():
        try:
            recebido = datetime.fromisoformat(recebido_em.strip())
        except ValueError:
            recebido = None
    if recebido is not None and recebido.tzinfo is None:
        recebido = recebido.replace(tzinfo=TZ)
    return data_referencia(None, recebido if recebido is not None else agora)


def _unidade(bruta: object) -> str | None:
    if not isinstance(bruta, str):
        return None
    return bruta.strip().upper() or None


def avaliar(
    order: Order,
    check: dict,
    *,
    cnpj_cliente: str | None,
    resolver_rede: Callable[[str], Rede | None],
    preco_vigente: Callable[[str | None, str, date], PrecoLinha | None],
    cnpjs_grupo: set[str],
    ultima_sync: datetime | None,
    agora: datetime,
    data_ref: date,
) -> Avaliacao:
    """`preco_vigente(rede_id, erp_produto_id, data)` com a empresa já fixada;
    pode levantar `PrecoInvalido`. `data_ref` decide a vigência; `agora`, só o
    frescor da cópia. Precedência na fila (§4.1): rede_travada >
    pedido_sem_preco > sem_preco_tabela > preco_acima | preco_abaixo; antes
    dela, o que não é avaliado (bloqueia): produto, rede, preço inválido e
    unidade."""
    cnpj_rede = cnpj_da_rede(order, cnpj_cliente)
    check_itens = check.get("items") or []

    def _id(i: int) -> str | None:
        if i >= len(check_itens):
            return None
        bruto = check_itens[i].get("fire_product_id")
        return None if bruto is None else str(bruto)

    def _un(i: int) -> str | None:
        return _unidade(check_itens[i].get("fire_unidade")) if i < len(check_itens) else None

    def _tudo(situacao: Situacao, motivo: Motivo, rede: Rede | None = None):
        return Avaliacao(
            rede=rede,
            cnpj_rede=cnpj_rede,
            itens=[
                ItemAvaliado(
                    i, _id(i), situacao, motivo, centavos(it.unit_price), None, None, _un(i)
                )
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
    itens: list[ItemAvaliado] = []
    for i, it in enumerate(order.items):
        pid = _id(i)
        unidade = _un(i)
        pedido = centavos(it.unit_price)

        def _item(situacao: Situacao, motivo: Motivo, linha: PrecoLinha | None = None):
            return ItemAvaliado(
                i,
                pid,
                situacao,
                motivo,
                pedido,
                linha.preco_centavos if linha else None,
                linha.preco_id if linha else None,
                unidade,
            )

        if pid is None:
            itens.append(_item("nao_avaliado", "produto_nao_identificado"))
            continue
        if rede is None:
            itens.append(_item("nao_avaliado", "cliente_sem_rede"))
            continue
        try:
            linha = preco_vigente(rede.id, pid, data_ref)
        except PrecoInvalido:
            itens.append(_item("nao_avaliado", "preco_invalido"))
            continue
        if linha is not None and linha.preco_centavos <= 0:
            linha = None  # dinheiro do contrato é > 0: zero não é preço
        if unidade is None or (linha is not None and _unidade(linha.unidade_erp) != unidade):
            itens.append(_item("nao_avaliado", "unidade_divergente"))
        elif rede.travada:
            itens.append(_item("fila", "rede_travada", linha))
        elif pedido is None:
            itens.append(_item("fila", "pedido_sem_preco", linha))
        elif linha is None:
            itens.append(_item("fila", "sem_preco_tabela"))
        elif pedido == linha.preco_centavos:
            itens.append(_item("ok", "ok", linha))
        elif pedido > linha.preco_centavos:
            itens.append(_item("fila", "preco_acima", linha))
        else:
            itens.append(_item("fila", "preco_abaixo", linha))
    return Avaliacao(rede=rede, cnpj_rede=cnpj_rede, itens=itens)
