# app/erp/depara_cliente.py
"""De-para de cliente intercompany: PEDIDO_CLIENTE → cliente real da revenda.

Alguns pedidos da produção (.7 Americanense) saem no nome da Nasmar, que é
revenda: ela fatura, mas quem recebe é o cliente final (Studio Z, Beira Rio,
Dakota). O número do pedido de compra do cliente final (PEDIDO_CLIENTE) é o
mesmo nos dois bancos — então ele resolve quem é o cliente de verdade.

Este módulo só LÊ o Firebird da revenda e devolve o cliente. Não conhece
`Order`, não conhece Flow e não decide quando deve ser usado (isso é
`app/integrations/flowpcp/intercompany.py`).
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

from app.erp.cnpj import cnpj_digits
from app.erp.connection import FirebirdConnection
from app.erp.queries import FIND_CLIENTE_REAL_BY_PEDIDO_CLIENTE
from app.persistence import environments_repo
from app.utils.logger import logger

# Janela de cool-down após `erro_conexao`, em segundos. `fdb` não expõe
# timeout — um host inalcançável trava a conexão por até ~180s. Sem isso,
# CADA preview/refresh/export durante uma queda do Firebird da revenda paga o
# stall inteiro, por clique, pra sempre (handlers síncronos num threadpool
# limitado). Não mexe no cache positivo nem passa a cachear `nao_encontrado`.
_ERRO_CONEXAO_COOLDOWN_S = 45.0

# Clock injetável — testes controlam o tempo sem sleep de parede real.
_clock = time.monotonic


@dataclass(frozen=True)
class ResolucaoCliente:
    """Resultado do de-para. `resolvido=False` ⇒ o chamador mantém a revenda.

    `motivo` ∈ `ok | sem_chave | nao_encontrado | ambiguo | sem_cnpj |
    config_invalida | erro_conexao`.
    """

    resolvido: bool
    cnpj: str | None = None
    nome: str | None = None
    motivo: str = "sem_chave"
    # Radar da demanda fantasma: STATUS/CODNF dos pedidos casados no .4.
    pedidos_no_4: list[dict] = field(default_factory=list)
    # Qual ambiente (Firebird) respondeu — presente em TODO caminho, inclusive
    # falha. É a única forma de auditar depois "quais pedidos foram resolvidos
    # sob uma config errada" se intercompany_env_slug for corrigido mais tarde.
    revenda_slug: str = ""


# Cache de processo. Só guarda resolução POSITIVA: o vínculo chave→cliente é
# fato histórico. Negativo nunca entra — o pedido pode ser criado na revenda
# depois, e o servidor web fica de pé por dias.
_CACHE: dict[tuple[str, str], ResolucaoCliente] = {}

# Cool-down por revenda_slug: timestamp (`_clock()`) da última `erro_conexao`.
# Só isso — nunca guarda motivo positivo nem `nao_encontrado`.
_FALHA_RECENTE: dict[str, float] = {}


def limpar_cache() -> None:
    """Zera o cache de processo e o cool-down de falha (testes/reconfiguração)."""
    _CACHE.clear()
    _FALHA_RECENTE.clear()


def resolver_cliente_real(chave: str | None, *, revenda_slug: str) -> ResolucaoCliente:
    """Traduz a chave (PEDIDO_CLIENTE) no cliente real cadastrado na revenda.

    Nunca levanta: qualquer falha vira `resolvido=False` com o motivo.
    """
    chave_limpa = (chave or "").strip()
    if not chave_limpa:
        return ResolucaoCliente(False, motivo="sem_chave", revenda_slug=revenda_slug)

    cache_key = (revenda_slug, chave_limpa)
    if cache_key in _CACHE:
        return _CACHE[cache_key]

    falha_em = _FALHA_RECENTE.get(revenda_slug)
    if falha_em is not None and (_clock() - falha_em) < _ERRO_CONEXAO_COOLDOWN_S:
        # Cool-down ativo: nem tenta a rede. Uma queda do Firebird da revenda
        # não pode custar um stall de dezenas de segundos por clique.
        return ResolucaoCliente(False, motivo="erro_conexao", revenda_slug=revenda_slug)

    try:
        env = environments_repo.get_by_slug(revenda_slug)
    except Exception as exc:  # noqa: BLE001 — best-effort: fallback pra revenda
        logger.warning(f"depara_cliente: lookup do ambiente '{revenda_slug}' falhou: {exc}")
        _FALHA_RECENTE[revenda_slug] = _clock()
        return ResolucaoCliente(False, motivo="erro_conexao", revenda_slug=revenda_slug)

    if env is None:
        logger.warning(f"depara_cliente: ambiente de revenda '{revenda_slug}' não existe")
        return ResolucaoCliente(False, motivo="config_invalida", revenda_slug=revenda_slug)

    try:
        cfg = environments_repo.to_fb_config(env)
        with FirebirdConnection().connect_with_config(cfg) as conn:
            cur = conn.cursor()
            try:
                cur.execute(FIND_CLIENTE_REAL_BY_PEDIDO_CLIENTE, (chave_limpa,))
                rows = cur.fetchall()
            finally:
                cur.close()
    except Exception as exc:  # noqa: BLE001 — best-effort: fallback pra revenda
        logger.warning(f"depara_cliente: leitura da revenda falhou (chave={chave_limpa!r}): {exc}")
        _FALHA_RECENTE[revenda_slug] = _clock()
        return ResolucaoCliente(False, motivo="erro_conexao", revenda_slug=revenda_slug)

    # Conexão respondeu — o Firebird da revenda está de pé. Limpa qualquer
    # cool-down anterior pra esse slug (independente do que `_decidir` decidir).
    _FALHA_RECENTE.pop(revenda_slug, None)

    resultado = _decidir(rows, revenda_slug=revenda_slug)
    if resultado.resolvido:
        _CACHE[cache_key] = resultado
    return resultado


def _decidir(rows: list, *, revenda_slug: str) -> ResolucaoCliente:
    """Regra pura: só resolve com UM CNPJ distinto, válido, entre os hits.

    Vários pedidos podem dividir o mesmo PEDIDO_CLIENTE na revenda (2 a 4 é
    comum) — isso não é ambiguidade enquanto apontarem pro mesmo CNPJ.
    """
    if not rows:
        return ResolucaoCliente(False, motivo="nao_encontrado", revenda_slug=revenda_slug)

    pedidos = [{"codigo": r[0], "status": r[1], "codnf": r[2]} for r in rows]
    cnpjs = {cnpj_digits(r[6]) for r in rows}
    if len(cnpjs) > 1:
        logger.warning(f"depara_cliente: ambíguo, {len(cnpjs)} CNPJs distintos — mantendo revenda")
        return ResolucaoCliente(
            False, motivo="ambiguo", pedidos_no_4=pedidos, revenda_slug=revenda_slug
        )

    cnpj = next(iter(cnpjs))
    # CADASTRO.CPF_CNPJ é campo legado: linhas com "ISENTO", "0" ou cadastro
    # meio digitado passam batidas no dígitos-only e o Flow rejeita (400) —
    # length 11 (CPF) ou 14 (CNPJ) é o único formato que o contrato aceita.
    # Isso também cobre o caso de CNPJ em branco num match único (antes
    # reportado como "ambiguo", o que não fazia sentido para 1 hit só).
    if len(cnpj) not in (11, 14):
        logger.warning(f"depara_cliente: CPF_CNPJ inválido ({cnpj!r}) na revenda — sem_cnpj")
        return ResolucaoCliente(
            False, motivo="sem_cnpj", pedidos_no_4=pedidos, revenda_slug=revenda_slug
        )

    primeira = rows[0]
    nome = (primeira[5] or "").strip() or (primeira[4] or "").strip() or None
    return ResolucaoCliente(
        True, cnpj=cnpj, nome=nome, motivo="ok", pedidos_no_4=pedidos, revenda_slug=revenda_slug
    )
