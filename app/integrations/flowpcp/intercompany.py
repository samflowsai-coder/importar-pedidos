# app/integrations/flowpcp/intercompany.py
"""Política do de-para de cliente intercompany.

Decide SE o de-para se aplica a um pedido; a leitura do Firebird da revenda
é do `app/erp/depara_cliente.py`. Nunca levanta: o push é best-effort e um
pedido com a revenda no lugar do cliente é melhor que push derrubado.
"""

from __future__ import annotations

from app.erp.cnpj import cnpj_digits
from app.erp.depara_cliente import ResolucaoCliente, resolver_cliente_real
from app.models.order import Order
from app.persistence import environments_repo
from app.utils.logger import logger


def resolucao_para(order: Order, *, slug: str) -> ResolucaoCliente | None:
    """Resolve o cliente real quando o pedido está no nome da revenda.

    Devolve `None` quando o de-para NÃO se aplica: ambiente sem config, ou
    cliente do pedido diferente do CNPJ intercompany. Nesse caso o chamador
    segue com o payload de sempre e não audita nada.
    """
    env = environments_repo.get_by_slug(slug)
    if env is None:
        return None

    alvo = cnpj_digits(env.get("intercompany_cnpj"))
    revenda_slug = (env.get("intercompany_env_slug") or "").strip()
    if not alvo or not revenda_slug:
        return None

    if cnpj_digits(order.header.customer_cnpj) != alvo:
        return None

    try:
        return resolver_cliente_real(order.header.order_number, revenda_slug=revenda_slug)
    except Exception as exc:  # noqa: BLE001 — o resolver já engole tudo; cinto e suspensório
        logger.warning(f"intercompany: resolver falhou (import slug={slug}): {exc}")
        return ResolucaoCliente(False, motivo="erro_conexao")
