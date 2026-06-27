"""Hook de push de pedido novo pro FlowPCP (Modelo B / OVERLAY).

Chamado no fim do envio ao Fire (`SEND_TO_FIRE_SUCCEEDED`), só em ambientes com
FlowPCP habilitado (MM). Best-effort: o pedido JÁ entrou no Fire — uma falha
aqui vira outbox/retry e nunca pode derrubar o fluxo de send-to-fire.
"""
from __future__ import annotations

from app.integrations.flowpcp.client import FlowPCPClient
from app.integrations.flowpcp.config import flowpcp_config_for_slug
from app.integrations.flowpcp.exporter import FlowPCPExporter
from app.models.order import Order
from app.utils.logger import logger


def push_new_order(order: Order, *, import_id: str, slug: str) -> bool:
    """Notifica o FlowPCP de um pedido novo. Retorna True se enviado; False se
    o ambiente não tem FlowPCP habilitado, ou se o envio falhou (já enfileirado
    no outbox para retry). Nunca levanta exceção."""
    cfg = flowpcp_config_for_slug(slug)
    if cfg is None:
        return False
    try:
        client = FlowPCPClient(
            base_url=cfg.base_url,
            service_token=cfg.service_token,
            tenant_id=cfg.tenant_id,
            timeout=cfg.request_timeout_s,
        )
        try:
            return FlowPCPExporter(client, tenant_id=cfg.tenant_id).export(
                order, import_id=import_id
            )
        finally:
            client.close()
    except Exception as exc:  # noqa: BLE001 — best-effort; nunca derruba o send-to-fire
        logger.warning(f"flowpcp push falhou (import={import_id} slug={slug}): {exc}")
        return False
