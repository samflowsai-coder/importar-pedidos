from __future__ import annotations

from app.erp.depara_cliente import ResolucaoCliente
from app.integrations.flowpcp.client import FLOWPCP_TARGET_NAME, RECEBIMENTO_PATH
from app.integrations.flowpcp.mapper import build_recebimento_payload
from app.models.order import Order
from app.persistence import outbox_repo
from app.utils.logger import logger


class FlowPCPExporter:
    """Enfileira o pedido no outbox (target=flowpcp). O worker (drain_outbox)
    entrega e faz retry. Sem HTTP no request path — a UI não espera o Flow."""

    def __init__(self, *, tenant_id: str) -> None:
        self._tenant_id = tenant_id

    def enqueue(
        self, order: Order, *, import_id: str, resolucao: ResolucaoCliente | None = None
    ) -> bool:
        req = build_recebimento_payload(
            import_id=import_id, order=order, tenant_id=self._tenant_id, resolucao=resolucao
        )
        try:
            outbox_repo.enqueue(
                import_id=import_id,
                target=FLOWPCP_TARGET_NAME,
                endpoint=RECEBIMENTO_PATH,
                payload=req.model_dump(by_alias=True),
                idempotency_key=f"send-{import_id}",
            )
            return True
        except outbox_repo.OutboxDuplicateError:
            logger.info(f"flowpcp já enfileirado (import={import_id}) — no-op")
            return True
