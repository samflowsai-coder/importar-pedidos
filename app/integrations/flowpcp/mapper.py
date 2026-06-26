from __future__ import annotations

from datetime import UTC, datetime

from app.integrations.flowpcp.schema import (
    ClienteRecebimento,
    ItemRecebimento,
    OrigemRecebimento,
    RecebimentoRequest,
)
from app.models.order import Order

_IMPORTADOR_VERSAO = "1.0.0"


def _to_iso(br_date: str | None) -> str | None:
    if not br_date:
        return None
    try:
        return datetime.strptime(br_date, "%d/%m/%Y").strftime("%Y-%m-%dT00:00:00.000Z")
    except ValueError:
        return None


def build_recebimento_payload(*, import_id: str, order: Order, tenant_id: str) -> RecebimentoRequest:
    h = order.header
    itens = [
        ItemRecebimento(
            produtoCodigo=it.product_code or None,
            produtoEan=it.ean or None,
            descricao=it.description,
            quantidade=float(it.quantity),
            precoUnitario=float(it.unit_price) if it.unit_price is not None else None,
        )
        for it in order.items
    ]
    primeiro_prazo = _to_iso(order.items[0].delivery_date) if order.items else None
    return RecebimentoRequest(
        externalId=import_id,
        fornecedor=h.customer_name or "(sem fornecedor)",
        pedidoNumero=h.order_number or import_id,
        emitidoEm=_to_iso(h.issue_date)
        or datetime.now(UTC).strftime("%Y-%m-%dT00:00:00.000Z"),
        prazoSolicitado=primeiro_prazo,
        cliente=ClienteRecebimento(nome=h.customer_name or "(sem cliente)", cnpj=h.customer_cnpj or None),
        itens=itens,
        origem=OrigemRecebimento(
            importadorVersao=_IMPORTADOR_VERSAO,
            arquivoOriginal=order.source_file or "",
            parserUsado="importador",
            confiancaParser="alta",
        ),
    )
