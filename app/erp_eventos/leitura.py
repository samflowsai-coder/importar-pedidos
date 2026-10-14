"""Leitura do ERP para a vigia e para a foto. Só SELECT (`queries.py`, bloco SP1),
sempre dentro de `FirebirdConnection.connect_somente_leitura`.

Devolve `PedidoErp` crus: `cliente_real_cnpj` e `produto_produtor` saem None
e são preenchidos pela vigia (cada um tem I/O próprio).
"""

from __future__ import annotations

from collections import defaultdict
from datetime import date, datetime
from decimal import Decimal

from app.erp import queries
from app.erp.cnpj import cnpj_digits
from app.erp_eventos.modelo import ItemErp, PedidoErp

SEM_STATUS = "(SEM STATUS)"

# Itens por ida ao banco (mesmo limite da reconciliação: SQL curto e previsível).
BLOCO = 200


def _data(v) -> date | None:
    if v is None:
        return None
    if isinstance(v, datetime):
        return v.date()
    if isinstance(v, date):
        return v
    texto = str(v).strip()
    return date.fromisoformat(texto[:10]) if texto else None


def _dec(v) -> Decimal | None:
    return None if v is None else Decimal(str(v))


def _texto(v) -> str | None:
    if v is None:
        return None
    t = str(v).strip()
    return t or None


def _cnpj(v) -> str | None:
    """Só CNPJ de 14 dígitos vai ao contrato; CPF, 'ISENTO' e vazio viram None."""
    d = cnpj_digits(v)
    return d if len(d) == 14 else None


def _em_blocos(cur, sql_de_n, chaves: list) -> list[tuple]:
    linhas: list[tuple] = []
    for i in range(0, len(chaves), BLOCO):
        bloco = chaves[i : i + BLOCO]
        cur.execute(sql_de_n(len(bloco)), bloco)
        linhas.extend(cur.fetchall())
    return linhas


def _montar(cur, cabecalhos: list[tuple]) -> dict[int, PedidoErp]:
    codigos = [int(c[0]) for c in cabecalhos]
    itens_por_pedido: dict[int, list[tuple]] = defaultdict(list)
    for r in _em_blocos(cur, queries.itens_erp_por_pedido_sql, codigos) if codigos else []:
        itens_por_pedido[int(r[1])].append(r)
    codigos_item = [int(r[0]) for rs in itens_por_pedido.values() for r in rs]
    faturado: dict[int, tuple[Decimal, int | None]] = {}
    for r in (
        _em_blocos(cur, queries.faturado_erp_por_item_sql, codigos_item) if codigos_item else []
    ):
        faturado[int(r[0])] = (_dec(r[1]) or Decimal(0), int(r[2]) if r[2] is not None else None)

    saida: dict[int, PedidoErp] = {}
    for c in cabecalhos:
        codigo = int(c[0])
        itens = []
        for r in itens_por_pedido.get(codigo, []):
            item_codigo = int(r[0])
            qtd_fat, cod_nf = faturado.get(item_codigo, (Decimal(0), None))
            itens.append(
                ItemErp(
                    erp_item_codigo=item_codigo,
                    cod_produto=int(r[2]) if r[2] is not None else None,
                    produto_produtor=None,
                    descricao=_texto(r[3]) or "",
                    qtd=_dec(r[4]),
                    qtd_faturada=qtd_fat,
                    cod_nf=cod_nf,
                    preco_unitario=_dec(r[5]),
                    unid=_texto(r[6]),
                    dt_entrega_item=_data(r[7]),
                )
            )
        saida[codigo] = PedidoErp(
            erp_codigo=codigo,
            pedido_cliente=_texto(c[1]),
            cliente_erp_codigo=int(c[2]) if c[2] is not None else 0,
            cliente_erp_cnpj=_cnpj(c[3]),
            cliente_real_cnpj=None,
            # STATUS vazio no ERP: o contrato exige texto; marca explícita, nunca inventada.
            status=_texto(c[4]) or SEM_STATUS,
            data_pedido=_data(c[5]),
            dt_entrega=_data(c[6]),
            itens=tuple(sorted(itens, key=lambda i: i.erp_item_codigo)),
        )
    return saida


class LeitorFirebird:
    """`LeitorErp` sobre uma conexão aberta com `connect_somente_leitura`."""

    def __init__(self, conn) -> None:
        self._conn = conn

    def abertos(self) -> list[PedidoErp]:
        cur = self._conn.cursor()
        try:
            cur.execute(queries.LIST_PEDIDOS_ABERTOS_ERP)
            return list(_montar(cur, cur.fetchall()).values())
        finally:
            cur.close()

    def por_codigo(self, codigos: list[int]) -> dict[int, PedidoErp]:
        if not codigos:
            return {}
        cur = self._conn.cursor()
        try:
            return _montar(cur, _em_blocos(cur, queries.pedidos_erp_por_codigo_sql, list(codigos)))
        finally:
            cur.close()
