"""Figura fiscal do pedido no insert direto: a que a própria empresa usa para o cliente.

A nota do Fire sai da figura do cabeçalho (`CAB_VENDAS.CODFIGFISCAL`): CFOP,
alíquota e redução da nota são os da `FIGURA_FISCAL` dela, não os do item do
pedido (medido em 2026-10-26 contra 433 notas autorizadas da .7: 4.129/4.129
itens). A tabela sozinha não escolhe: cada UF está em várias figuras (venda,
bonificação, remessa, não contribuinte). Por isso a escolha segue o que as
operadoras da empresa já gravaram, em degraus, e nunca chuta. Só conta figura
de venda (CFOP 5.10x/6.10x); bonificação, remessa e devolução ficam de fora.

1. `cliente`: se ele tem pedido de venda na janela com figura que cobre a UF
   atual dele, só esse histórico decide: uma figura com DOMINANCIA. Dividido
   recusa (não cai para a UF: o cliente mostrou que a figura dele não é a da
   UF). Figura de outra UF (cliente que mudou de estado) não conta;
2. `uf`: cliente sem esse histórico; na empresa, uma figura tem ao menos
   MIN_PEDIDOS_UF pedidos e DOMINANCIA dos pedidos de venda da UF dele;
3. `padrao`: a figura do ambiente (`fiscal_codfigfiscal`), se cobre a UF;
4. nenhum: `FirebirdFiguraFiscalIndefinidaError` e o pedido não é gravado.

A figura escolhida precisa ter a UF do cliente no `UF_DESTINO`. O histórico
ignora pedidos do próprio portal e cancelados.
"""

from __future__ import annotations

import dataclasses
import re
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal

from app.erp import queries
from app.erp.exceptions import FirebirdFiguraFiscalIndefinidaError
from app.erp.fiscal import PerfilFiscal
from app.erp.mapper import FireSistemasMapper
from app.utils.logger import logger

JANELA_DIAS = 365
MIN_PEDIDOS_UF = 3
DOMINANCIA = Decimal("0.9")
_CFOP_VENDA = re.compile(r"^[56]\.10\d$")


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


def e_venda(f: Figura | None) -> bool:
    return f is not None and bool(_CFOP_VENDA.match(f.cfop))


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

    def serve(codigo: int | None) -> bool:
        f = figura(codigo) if codigo is not None else None
        return e_venda(f) and uf in f.ufs

    def dominante(hist: dict[int, int], minimo: int) -> int | None:
        vendas = {c: n for c, n in hist.items() if e_venda(figura(c))}
        total = sum(vendas.values())
        if not total:
            return None
        codigo, n = max(vendas.items(), key=lambda kv: kv[1])
        return codigo if n >= minimo and Decimal(n) / total >= DOMINANCIA else None

    do_cliente = historico_na_uf(por_cliente, uf, figura)
    if do_cliente:
        codigo = dominante(do_cliente, 1)
        return (codigo, "cliente") if serve(codigo) else None

    codigo = dominante(por_uf, MIN_PEDIDOS_UF)
    if serve(codigo):
        return codigo, "uf"
    if serve(padrao):
        return padrao, "padrao"
    return None


def historico_na_uf(
    hist: dict[int, int], uf: str, figura: Callable[[int], Figura | None]
) -> dict[int, int]:
    """Só os pedidos com figura de venda que cobre `uf`."""
    uf = (uf or "").strip().upper()
    return {c: n for c, n in hist.items() if e_venda(f := figura(c)) and uf in f.ufs}


def resolver(cur, cliente: int, perfil: PerfilFiscal) -> PerfilFiscal:
    """Perfil do pedido: figura escolhida e CFOP/ICMS/redução da `FIGURA_FISCAL` dela."""
    cur.execute(queries.FIND_CLIENT_UF, (cliente,))
    row = cur.fetchone()
    uf = (row[0] or "").strip().upper() if row else ""

    if not uf:
        raise FirebirdFiguraFiscalIndefinidaError(cliente, uf)
    desde = date.today() - timedelta(days=JANELA_DIAS)
    sistema = FireSistemasMapper.USUARIO_SISTEMA
    cur.execute(queries.FIGURAS_DO_CLIENTE, (cliente, desde, sistema))
    por_cliente = {int(c): int(n) for c, n in cur.fetchall()}

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

    por_uf: dict[int, int] = {}
    if not historico_na_uf(por_cliente, uf, figura):  # só consulta a UF se o cliente não decide
        cur.execute(queries.FIGURAS_DA_UF, (uf, desde, sistema))
        por_uf = {int(c): int(n) for c, n in cur.fetchall()}

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
