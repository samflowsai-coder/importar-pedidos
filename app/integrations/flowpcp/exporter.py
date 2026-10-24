from __future__ import annotations

from app.erp.depara_cliente import ResolucaoCliente
from app.integrations.flowpcp import cliente_matriz, recebimento_por_loja
from app.integrations.flowpcp.client import FLOWPCP_TARGET_NAME, RECEBIMENTO_PATH
from app.integrations.flowpcp.mapper import build_recebimento_payload, build_recebimentos_por_loja
from app.models.order import Order
from app.persistence import outbox_repo
from app.utils.logger import logger


class FlowPCPExporter:
    """Enfileira o pedido no outbox (target=flowpcp). O worker (drain_outbox)
    entrega e faz retry. Sem HTTP no request path — a UI não espera o Flow."""

    def __init__(self, *, tenant_id: str) -> None:
        self._tenant_id = tenant_id

    def enqueue(
        self,
        order: Order,
        *,
        import_id: str,
        resolucao: ResolucaoCliente | None = None,
        slug: str | None = None,
    ) -> bool:
        matriz = cliente_matriz.para_pedido(slug, order, resolucao)
        por_loja = (
            matriz is None
            and bool(slug)
            and recebimento_por_loja.ligado(slug)
            and recebimento_por_loja.deve_dividir(order, resolucao)
        )
        if self._formato_diferente_ja_enfileirado(import_id, por_loja=por_loja):
            return True
        if (
            matriz is None
            and slug
            and recebimento_por_loja.ligado(slug)
            and recebimento_por_loja.deve_dividir(order, resolucao)
        ):
            return self._enqueue_por_loja(order, import_id=import_id, resolucao=resolucao)
        req = build_recebimento_payload(
            import_id=import_id,
            order=order,
            tenant_id=self._tenant_id,
            resolucao=resolucao,
            cliente_matriz=matriz,
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

    @staticmethod
    def _formato_diferente_ja_enfileirado(import_id: str, *, por_loja: bool) -> bool:
        """Re-export com a configuração trocada no meio (matriz ou divisão por loja ligada depois do
        primeiro envio) não pode mandar o mesmo pedido ao Flow nos dois formatos: vale o que já está na
        fila."""
        unico = f"send-{import_id}"
        chaves = [
            r.idempotency_key
            for r in outbox_repo.list_for_import(import_id, limit=500)
            if r.target == FLOWPCP_TARGET_NAME
        ]
        ja_unico = unico in chaves
        ja_por_loja = any(k.startswith(unico + "-") for k in chaves)
        if (por_loja and ja_unico) or (not por_loja and ja_por_loja):
            logger.warning(
                f"flowpcp: import={import_id} já foi enfileirado no outro formato "
                f"({'único' if ja_unico else 'por loja'}); nada novo enfileirado"
            )
            return True
        return False

    def _enqueue_por_loja(
        self, order: Order, *, import_id: str, resolucao: ResolucaoCliente | None
    ) -> bool:
        """Uma linha por loja, chave de idempotência por loja. Reenfileirar é no-op por linha."""
        for req in build_recebimentos_por_loja(
            import_id=import_id, order=order, tenant_id=self._tenant_id, resolucao=resolucao
        ):
            try:
                outbox_repo.enqueue(
                    import_id=import_id,
                    target=FLOWPCP_TARGET_NAME,
                    endpoint=RECEBIMENTO_PATH,
                    payload=req.model_dump(by_alias=True),
                    idempotency_key=f"send-{import_id}-{req.cliente.cnpj}",
                )
            except outbox_repo.OutboxDuplicateError:
                logger.info(f"flowpcp loja {req.cliente.cnpj} já enfileirada (import={import_id})")
        return True
