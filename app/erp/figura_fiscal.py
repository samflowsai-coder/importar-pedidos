"""Figura fiscal do pedido no insert direto: a que a própria empresa usa para o cliente.

A nota do Fire sai da figura do cabeçalho (`CAB_VENDAS.CODFIGFISCAL`): CFOP,
alíquota e redução da nota são os da `FIGURA_FISCAL` dela, não os do item do
pedido (medido em 2026-10-26 contra 433 notas autorizadas da .7: 4.129/4.129
itens). A tabela sozinha não escolhe: cada UF está em várias figuras (venda,
bonificação, remessa, não contribuinte). Por isso a escolha segue o que as
operadoras da empresa já gravaram, em degraus, e nunca chuta:

1. `cliente`: os pedidos dele na janela usaram uma figura só;
2. `uf`: na empresa, uma figura tem ao menos MIN_PEDIDOS_UF pedidos e
   DOMINANCIA dos pedidos da UF do cliente;
3. `padrao`: a figura do ambiente (`fiscal_codfigfiscal`), se cobre a UF;
4. nenhum: `FirebirdFiguraFiscalIndefinidaError` e o pedido não é gravado.

Em todo degrau a figura precisa existir e ter a UF do cliente no `UF_DESTINO`;
senão o degrau é descartado. O histórico ignora pedidos do próprio portal
(`IMPORTADOR`) e cancelados.
"""

from __future__ import annotations

import dataclasses
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal

from app.erp import queries
from app.erp.exceptions import FirebirdFiguraFiscalIndefinidaError
from app.erp.fiscal import PerfilFiscal
from app.utils.logger import logger

JANELA_DIAS = 365
MIN_PEDIDOS_UF = 3
DOMINANCIA = Decimal("0.9")


@dataclass(frozen=True)
class Figura:
    codigo: int
    cfop: str
    icms: Decimal
    reducao: Decimal
    ufs: frozenset[str]


def ufs_de(uf_destino: str | None) -> frozenset[str]:
    """`;MG;PR;RJ;` do Fire -> {"MG", "PR", "RJ"}."""
    return frozenset(p.strip().upper() for p in (uf_destino or "").split(";") if p.strip())


def escolher(
    uf: str | None,
    por_cliente: dict[int, int],
    por_uf: dict[int, int],
    padrao: int | None,
    figura: Callable[[int], Figura | None],
) -> tuple[int, str] | None:
    """(figura, degrau) ou None. `por_cliente`/`por_uf`: {figura: nº de pedidos}."""
    uf = (uf or "").strip().upper()
    if not uf:
        return None

    def cobre(codigo: int | None) -> bool:
        f = figura(codigo) if codigo is not None else None
        return f is not None and uf in f.ufs

    if len(por_cliente) == 1:
        (codigo,) = por_cliente
        if cobre(codigo):
            return codigo, "cliente"

    total = sum(por_uf.values())
    if total:
        codigo, n = max(por_uf.items(), key=lambda kv: kv[1])
        if n >= MIN_PEDIDOS_UF and Decimal(n) / total >= DOMINANCIA and cobre(codigo):
            return codigo, "uf"

    if cobre(padrao):
        return padrao, "padrao"
    return None


def resolver(cur, cliente: int, perfil: PerfilFiscal) -> PerfilFiscal:
    """Perfil do pedido: figura escolhida e CFOP/ICMS/redução da `FIGURA_FISCAL` dela."""
    cur.execute(queries.FIND_CLIENT_UF, (cliente,))
    row = cur.fetchone()
    uf = (row[0] or "").strip().upper() if row else ""

    desde = date.today() - timedelta(days=JANELA_DIAS)
    cur.execute(queries.FIGURAS_DO_CLIENTE, (cliente, desde))
    por_cliente = {int(c): int(n) for c, n in cur.fetchall()}
    por_uf: dict[int, int] = {}
    if uf:
        cur.execute(queries.FIGURAS_DA_UF, (uf, desde))
        por_uf = {int(c): int(n) for c, n in cur.fetchall()}

    cache: dict[int, Figura | None] = {}

    def figura(codigo: int) -> Figura | None:
        if codigo not in cache:
            cur.execute(queries.FIGURA_FISCAL_POR_CODIGO, (codigo,))
            r = cur.fetchone()
            cache[codigo] = (
                None
                if r is None
                else Figura(
                    int(r[0]),
                    (r[1] or "").strip(),
                    Decimal(str(r[2] or 0)),
                    Decimal(str(r[3] or 0)),
                    ufs_de(r[4]),
                )
            )
        return cache[codigo]

    escolha = escolher(uf, por_cliente, por_uf, perfil.codfigfiscal, figura)
    if escolha is None:
        raise FirebirdFiguraFiscalIndefinidaError(cliente, uf)
    codigo, degrau = escolha
    f = figura(codigo)
    assert f is not None  # escolher só devolve figura que existe
    logger.info(f"Figura fiscal {codigo} ({degrau}) para cliente {cliente} UF={uf}")
    return dataclasses.replace(
        perfil, codfigfiscal=codigo, cfop_principal=f.cfop, icms_porc=f.icms, reducao=f.reducao
    )
