"""Regra comercial de faturamento MM x Nasmar: tipos da política.

Núcleo puro: `avaliar` decide pela rede fixa ou pela UF do CNPJ faturado e
devolve `None` sempre que não tem certeza (nunca chuta UF). A leitura da UF
entra por `ler_uf`, injetada.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from app.erp.cnpj import cnpj_digits
from app.models.order import Order


@dataclass(frozen=True)
class RedeFixa:
    """Rede que fatura sempre em `env_slug`, por raiz (8 díg.) ou CNPJ (14)."""

    nome: str
    env_slug: str
    raizes: frozenset[str]
    cnpjs: frozenset[str]


@dataclass(frozen=True)
class Politica:
    """Política ativa: empresa p/ destino em SP, fora de SP e redes fixas."""

    env_sp: str
    env_fora_sp: str
    redes: tuple[RedeFixa, ...]


@dataclass(frozen=True)
class UfLida:
    """UF(s) de um CNPJ numa base. `ufs` já normalizadas (strip+upper)."""

    env_slug: str
    ufs: frozenset[str]  # "" = linha sem UF; vazio = não achou o cliente
    indisponivel: bool = False


@dataclass(frozen=True)
class ResultadoRegra:
    env_slug: str
    explicacao: str


def rede_fixa_do_pedido(order: Order, politica: Politica) -> RedeFixa | None:
    """A única rede fixa a que os CNPJs do pedido pertencem; 0 ou 2+ → None."""
    from app.routing.ambiente import cnpjs_do_pedido

    achadas: list[RedeFixa] = []
    for cnpj in cnpjs_do_pedido(order):
        rede = next((r for r in politica.redes if cnpj in r.cnpjs), None) or next(
            (r for r in politica.redes if cnpj[:8] in r.raizes), None
        )
        if rede is not None and rede not in achadas:
            achadas.append(rede)
    return achadas[0] if len(achadas) == 1 else None


def avaliar(
    order: Order,
    politica: Politica,
    ler_uf: Callable[[str], tuple[UfLida, ...]],
) -> ResultadoRegra | None:
    """Rede fixa > UF do CNPJ faturado. Sem certeza → None (cai na escada)."""
    from app.routing.ambiente import _fmt

    rede = rede_fixa_do_pedido(order, politica)
    if rede is not None:
        return ResultadoRegra(
            rede.env_slug, f"Regra comercial: {rede.nome} fatura sempre por {rede.env_slug}"
        )
    cnpj = cnpj_digits(order.header.customer_cnpj)
    if not cnpj:
        return None
    leituras = ler_uf(cnpj)
    if any(le.indisponivel for le in leituras):
        return None
    ufs = frozenset().union(*(le.ufs for le in leituras))
    if len(ufs) != 1 or "" in ufs:
        return None
    (uf,) = ufs
    slug = politica.env_sp if uf == "SP" else politica.env_fora_sp
    return ResultadoRegra(slug, f"Regra comercial: CNPJ faturado {_fmt(cnpj)} em {uf} → {slug}")
