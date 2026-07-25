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

from dataclasses import dataclass, field

from app.erp.cnpj import cnpj_digits
from app.erp.connection import FirebirdConnection
from app.erp.queries import FIND_CLIENTE_REAL_BY_PEDIDO_CLIENTE
from app.persistence import environments_repo
from app.utils.logger import logger


@dataclass(frozen=True)
class ResolucaoCliente:
    """Resultado do de-para. `resolvido=False` ⇒ o chamador mantém a revenda."""

    resolvido: bool
    cnpj: str | None = None
    nome: str | None = None
    motivo: str = "sem_chave"
    # Radar da demanda fantasma: STATUS/CODNF dos pedidos casados no .4.
    pedidos_no_4: list[dict] = field(default_factory=list)


# Cache de processo. Só guarda resolução POSITIVA: o vínculo chave→cliente é
# fato histórico. Negativo nunca entra — o pedido pode ser criado na revenda
# depois, e o servidor web fica de pé por dias.
_CACHE: dict[tuple[str, str], ResolucaoCliente] = {}


def limpar_cache() -> None:
    """Zera o cache de processo (usado nos testes e em reconfiguração)."""
    _CACHE.clear()


def resolver_cliente_real(chave: str | None, *, revenda_slug: str) -> ResolucaoCliente:
    """Traduz a chave (PEDIDO_CLIENTE) no cliente real cadastrado na revenda.

    Nunca levanta: qualquer falha vira `resolvido=False` com o motivo.
    """
    chave_limpa = (chave or "").strip()
    if not chave_limpa:
        return ResolucaoCliente(False, motivo="sem_chave")

    cache_key = (revenda_slug, chave_limpa)
    if cache_key in _CACHE:
        return _CACHE[cache_key]

    try:
        env = environments_repo.get_by_slug(revenda_slug)
    except Exception as exc:  # noqa: BLE001 — best-effort: fallback pra revenda
        logger.warning(f"depara_cliente: lookup do ambiente '{revenda_slug}' falhou: {exc}")
        return ResolucaoCliente(False, motivo="erro_conexao")

    if env is None:
        logger.warning(f"depara_cliente: ambiente de revenda '{revenda_slug}' não existe")
        return ResolucaoCliente(False, motivo="config_invalida")

    try:
        cfg = environments_repo.to_fb_config(env)
        with FirebirdConnection().connect_with_config(cfg) as conn:
            rows = conn.execute(FIND_CLIENTE_REAL_BY_PEDIDO_CLIENTE, (chave_limpa,)).fetchall()
    except Exception as exc:  # noqa: BLE001 — best-effort: fallback pra revenda
        logger.warning(f"depara_cliente: leitura da revenda falhou (chave={chave_limpa!r}): {exc}")
        return ResolucaoCliente(False, motivo="erro_conexao")

    resultado = _decidir(rows)
    if resultado.resolvido:
        _CACHE[cache_key] = resultado
    return resultado


def _decidir(rows: list) -> ResolucaoCliente:
    """Regra pura: só resolve com UM CNPJ distinto entre os hits.

    Vários pedidos podem dividir o mesmo PEDIDO_CLIENTE na revenda (2 a 4 é
    comum) — isso não é ambiguidade enquanto apontarem pro mesmo CNPJ.
    """
    if not rows:
        return ResolucaoCliente(False, motivo="nao_encontrado")

    pedidos = [{"codigo": r[0], "status": r[1], "codnf": r[2]} for r in rows]
    cnpjs = {cnpj_digits(r[6]) for r in rows}
    if len(cnpjs) > 1:
        logger.warning(f"depara_cliente: ambíguo, {len(cnpjs)} CNPJs distintos — mantendo revenda")
        return ResolucaoCliente(False, motivo="ambiguo", pedidos_no_4=pedidos)

    cnpj = next(iter(cnpjs))
    if not cnpj:
        return ResolucaoCliente(False, motivo="ambiguo", pedidos_no_4=pedidos)

    primeira = rows[0]
    nome = (primeira[5] or "").strip() or (primeira[4] or "").strip() or None
    return ResolucaoCliente(True, cnpj=cnpj, nome=nome, motivo="ok", pedidos_no_4=pedidos)
