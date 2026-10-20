"""Regra comercial de faturamento MM x Nasmar: tipos da política.

Núcleo puro: `avaliar` decide pela rede fixa ou pela UF do CNPJ faturado e
devolve `None` sempre que não tem certeza (nunca chuta UF). A leitura da UF
entra por `ler_uf`, injetada.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field

from app.erp import queries
from app.erp.cnpj import cnpj_digits
from app.models.order import Order
from app.utils.logger import logger

# As 27 UFs + "EX" (exterior). Qualquer outro valor: a regra não responde.
UFS_VALIDAS = frozenset(
    "AC AL AM AP BA CE DF ES GO MA MG MS MT PA PB PE PI PR RJ RN RO RR RS SC SE SP TO EX".split()
)


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
    # slug -> nome da empresa, só para a explicação ("pela MM", não "por mm").
    nomes: dict[str, str] = field(default_factory=dict, hash=False, compare=False)
    # Slugs dos ambientes ativos; `None` = não conferido (testes puros). Rede
    # cuja empresa não está aqui não decide (nunca por slug fantasma).
    ativos: frozenset[str] | None = field(default=None, hash=False, compare=False)

    def nome(self, slug: str) -> str:
        return self.nomes.get(slug) or slug


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
    if rede is not None and politica.ativos is not None and rede.env_slug not in politica.ativos:
        # Nem a rede (empresa inexistente) nem a UF (a rede é exceção a ela).
        logger.warning(f"routing.regra.rede_empresa_inativa rede={rede.nome!r} env={rede.env_slug}")
        return None
    if rede is not None:
        return ResultadoRegra(
            rede.env_slug,
            f"Regra comercial: {rede.nome} fatura sempre pela {politica.nome(rede.env_slug)}",
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
    if uf not in UFS_VALIDAS:  # lixo no cadastro não vira "fora de SP"
        return None
    slug = politica.env_sp if uf == "SP" else politica.env_fora_sp
    return ResultadoRegra(
        slug, f"Regra comercial: CNPJ faturado {_fmt(cnpj)} em {uf} → {politica.nome(slug)}"
    )


def _uf_no_fire(env: dict, cnpj: str) -> list[str | None]:
    """UF(s) do CNPJ no Firebird do ambiente. Somente leitura."""
    from app.erp.connection import FirebirdConnection
    from app.persistence import environments_repo
    from app.routing.ambiente import _fmt

    cfg = environments_repo.to_fb_config(env)
    with FirebirdConnection().connect_somente_leitura(cfg) as conn:
        cur = conn.cursor()
        try:
            cur.execute(queries.UF_CLIENTE_POR_CNPJ, (_fmt(cnpj), cnpj))
            return [r[0] for r in cur.fetchall()]
        finally:
            cur.close()


def ler_uf(
    cnpj: str,
    *,
    envs: Sequence[dict],
    ler: Callable[[dict, str], list[str | None]] | None = None,
) -> tuple[UfLida, ...]:
    """Uma `UfLida` por env. Falha numa base = `indisponivel`, nunca chute."""
    leitor = ler or _uf_no_fire
    cnpj = cnpj_digits(cnpj)
    saida: list[UfLida] = []
    for env in envs:
        slug = env["slug"]
        try:
            valores = leitor(env, cnpj)
        except Exception as exc:  # noqa: BLE001 - base fora do ar = "não sei"
            logger.warning(f"routing.regra.uf_indisponivel env={slug} erro={exc!r}")
            saida.append(UfLida(slug, frozenset(), indisponivel=True))
            continue
        saida.append(UfLida(slug, frozenset((v or "").strip().upper() for v in valores)))
    return tuple(saida)


_TTL_CACHE_UF_S = 600.0
# Falha também fica em cache, curto: Fire fora do ar não paga um timeout de
# conexão por pedido, e volta a ser tentado em 1 minuto.
_TTL_CACHE_UF_FALHA_S = 60.0
_cache_uf: dict[tuple[str, tuple[str, ...]], tuple[float, tuple[UfLida, ...]]] = {}
_cache_uf_lock = threading.Lock()


def ler_uf_com_cache(cnpj: str, envs: Sequence[dict]) -> tuple[UfLida, ...]:
    """`ler_uf` com cache em memória: 600 s a leitura boa, 60 s a `indisponivel`."""
    cnpj = cnpj_digits(cnpj)
    chave = (cnpj, tuple(e["slug"] for e in envs))
    agora = time.monotonic()
    with _cache_uf_lock:
        hit = _cache_uf.get(chave)
        if hit:
            falhou = any(le.indisponivel for le in hit[1])
            ttl = _TTL_CACHE_UF_FALHA_S if falhou else _TTL_CACHE_UF_S
            if agora - hit[0] < ttl:
                return hit[1]
    leituras = ler_uf(cnpj, envs=envs)
    with _cache_uf_lock:
        _cache_uf[chave] = (agora, leituras)
    return leituras
