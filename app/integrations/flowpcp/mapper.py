from __future__ import annotations

from datetime import UTC, datetime

from app.erp.cnpj import cnpj_digits
from app.erp.depara_cliente import ResolucaoCliente
from app.integrations.flowpcp.schema import (
    ClienteRecebimento,
    FaturadoPor,
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


def build_recebimento_payload(
    *,
    import_id: str,
    order: Order,
    tenant_id: str,
    resolucao: ResolucaoCliente | None = None,
) -> RecebimentoRequest:
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

    # Intercompany: o Flow recebe o cliente REAL — é o CNPJ que resolve cliente
    # e marca do lado de lá — e quem fatura vai em faturadoPor. O `Order` não é
    # tocado: XLS e Fire continuam com a revenda, que é o certo fiscalmente.
    nome_cliente = h.customer_name or "(sem cliente)"
    cnpj_cliente = cnpj_digits(h.customer_cnpj) or None
    faturado_por = None
    if resolucao is not None and resolucao.resolvido:
        faturado_por = FaturadoPor(nome=nome_cliente, cnpj=cnpj_cliente)
        nome_cliente = resolucao.nome or nome_cliente
        cnpj_cliente = resolucao.cnpj or cnpj_cliente

    return RecebimentoRequest(
        externalId=import_id,
        fornecedor=h.customer_name or "(sem fornecedor)",
        pedidoNumero=h.order_number or import_id,
        emitidoEm=_to_iso(h.issue_date) or datetime.now(UTC).strftime("%Y-%m-%dT00:00:00.000Z"),
        prazoSolicitado=primeiro_prazo,
        cliente=ClienteRecebimento(nome=nome_cliente, cnpj=cnpj_cliente),
        faturadoPor=faturado_por,
        itens=itens,
        origem=OrigemRecebimento(
            importadorVersao=_IMPORTADOR_VERSAO,
            arquivoOriginal=order.source_file or "",
            parserUsado="importador",
            confiancaParser="alta",
        ),
    )
