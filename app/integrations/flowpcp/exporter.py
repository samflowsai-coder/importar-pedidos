from __future__ import annotations

from app.integrations.flowpcp.client import (
    FLOWPCP_TARGET_NAME,
    RECEBIMENTO_PATH,
    FlowPCPClient,
)
from app.integrations.flowpcp.mapper import build_recebimento_payload
from app.models.order import Order
from app.persistence import outbox_repo
from app.utils.logger import logger


class FlowPCPExporter:
    def __init__(self, client: FlowPCPClient, *, tenant_id: str) -> None:
        self._client = client
        self._tenant_id = tenant_id

    def export(self, order: Order, *, import_id: str) -> bool:
        req = build_recebimento_payload(
            import_id=import_id, order=order, tenant_id=self._tenant_id
        )
        try:
            self._client.send_order(req, idempotency_key=f"send-{import_id}")
            return True
        except Exception as exc:  # noqa: BLE001 — falha vira retry via outbox
            logger.warning(
                f"flowpcp send_order falhou (import={import_id}): {exc} — enfileirando outbox"
            )
            outbox_repo.enqueue(
                import_id=import_id,
                target=FLOWPCP_TARGET_NAME,
                endpoint=RECEBIMENTO_PATH,
                payload=req.model_dump(by_alias=True),
                idempotency_key=f"send-{import_id}",
            )
            return False
