"""Hook de push de pedido novo pro FlowPCP (Modelo B / OVERLAY).

Chamado no fim do envio ao Fire (`SEND_TO_FIRE_SUCCEEDED`), só em ambientes com
FlowPCP habilitado (MM). Best-effort: o pedido JÁ entrou no Fire — uma falha
aqui vira outbox/retry e nunca pode derrubar o fluxo de send-to-fire.
"""

from __future__ import annotations

from app.integrations.flowpcp.config import flowpcp_config_for_slug
from app.integrations.flowpcp.exporter import FlowPCPExporter
from app.models.order import Order
from app.utils.logger import logger


def push_new_order(order: Order, *, import_id: str, slug: str) -> bool:
    """Enfileira o pedido pro FlowPCP (outbox). Retorna True se enfileirado;
    False se o ambiente não tem FlowPCP habilitado ou se o enqueue falhou.
    Best-effort: nunca levanta — o send-to-fire já teve sucesso."""
    cfg = flowpcp_config_for_slug(slug)
    if cfg is None:
        return False
    try:
        return FlowPCPExporter(tenant_id=cfg.tenant_id).enqueue(order, import_id=import_id)
    except Exception as exc:  # noqa: BLE001 — best-effort; nunca derruba o send-to-fire
        logger.warning(f"flowpcp enqueue falhou (import={import_id} slug={slug}): {exc}")
        return False
