"""Hook de push de pedido novo pro FlowPCP (Modelo B / OVERLAY).

Chamado no fim do envio ao Fire (`SEND_TO_FIRE_SUCCEEDED`), só em ambientes com
FlowPCP habilitado (MM). Best-effort: o pedido JÁ entrou no Fire — uma falha
aqui vira outbox/retry e nunca pode derrubar o fluxo de send-to-fire.
"""

from __future__ import annotations

from app.erp.depara_cliente import ResolucaoCliente
from app.integrations.flowpcp.client import FlowPCPClient
from app.integrations.flowpcp.config import flowpcp_config_for_slug
from app.integrations.flowpcp.exporter import FlowPCPExporter
from app.integrations.flowpcp.intercompany import resolucao_para
from app.models.order import Order
from app.persistence import repo
from app.utils.logger import logger


def _resolucao_segura(order: Order, *, import_id: str, slug: str) -> ResolucaoCliente | None:
    """Chama `resolucao_para` sob guard próprio — cinto e suspensório.

    `resolucao_para` já nunca levanta (best-effort desde a leitura do
    ambiente), mas `push_new_order` é o contrato que os call sites em
    `app/web/server.py` dependem diretamente (chamado logo após o Fire/XLS já
    ter tido sucesso). Este guard garante que o contrato se sustenta mesmo
    que uma regressão futura reintroduza um caminho sem proteção lá dentro.
    """
    try:
        return resolucao_para(order, slug=slug)
    except Exception as exc:  # noqa: BLE001 — nunca pode derrubar o push
        logger.warning(
            f"intercompany: resolucao_para falhou (import={import_id} slug={slug}): {exc}"
        )
        return None


def _auditar_resolucao(order: Order, *, import_id: str, resolucao: ResolucaoCliente) -> None:
    try:
        repo.append_audit(
            import_id,
            "depara_cliente",
            {
                "chave": order.header.order_number,
                "motivo": resolucao.motivo,
                "resolvido": resolucao.resolvido,
                "cnpj_real": resolucao.cnpj,
                "nome_real": resolucao.nome,
                # Radar da demanda fantasma — o pedido casado na revenda pode
                # já estar FATURADO. Só observa; não bloqueia.
                "pedidos_no_4": resolucao.pedidos_no_4,
            },
        )
    except Exception as exc:  # noqa: BLE001 — auditar não pode derrubar o push
        logger.warning(f"intercompany: audit falhou (import={import_id}): {exc}")


def push_new_order(order: Order, *, import_id: str, slug: str) -> bool:
    """Notifica o FlowPCP de um pedido novo. Retorna True se enviado; False se
    o ambiente não tem FlowPCP habilitado, ou se o envio falhou (já enfileirado
    no outbox para retry). Nunca levanta exceção."""
    cfg = flowpcp_config_for_slug(slug)
    if cfg is None:
        return False

    resolucao = _resolucao_segura(order, import_id=import_id, slug=slug)
    if resolucao is not None:
        _auditar_resolucao(order, import_id=import_id, resolucao=resolucao)

    try:
        client = FlowPCPClient(
            base_url=cfg.base_url,
            service_token=cfg.service_token,
            tenant_id=cfg.tenant_id,
            timeout=cfg.request_timeout_s,
        )
        try:
            return FlowPCPExporter(client, tenant_id=cfg.tenant_id).export(
                order, import_id=import_id, resolucao=resolucao
            )
        finally:
            client.close()
    except Exception as exc:  # noqa: BLE001 — best-effort; nunca derruba o send-to-fire
        logger.warning(f"flowpcp push falhou (import={import_id} slug={slug}): {exc}")
        return False
