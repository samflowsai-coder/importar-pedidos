from __future__ import annotations

from datetime import UTC, datetime

from app.erp.cnpj import cnpj_digits
from app.erp.depara_cliente import ResolucaoCliente
from app.integrations.flowpcp import recebimento_por_loja
from app.integrations.flowpcp.cliente_matriz import ClienteMatriz
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
    cliente_matriz: ClienteMatriz | None = None,
) -> RecebimentoRequest:
    h = order.header
    itens = [
        ItemRecebimento(
            produtoCodigo=it.product_code or None,
            produtoEan=it.ean or None,
            descricao=it.description,
            quantidade=float(it.quantity),
            precoUnitario=float(it.unit_price) if it.unit_price is not None else None,
            erpProdutoId=it.erp_produto_id,
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
        # O Firebird da revenda pode ter NOME e RAZAO_SOCIAL em branco — um
        # cliente real sem nome cadastrado. Nesse caso o CNPJ resolvido NUNCA
        # pode viajar pareado com o nome da revenda (quem faturou): isso seria
        # uma identidade mista (CNPJ certo, nome de quem faturou) e o Flow
        # poderia cadastrar o cliente novo com o nome errado. Cai num rótulo
        # neutro derivado do próprio CNPJ resolvido.
        if resolucao.cnpj:
            cnpj_cliente = resolucao.cnpj
            nome_cliente = resolucao.nome or f"Cliente {resolucao.cnpj}"
        elif resolucao.nome:
            nome_cliente = resolucao.nome

    if cliente_matriz is not None:
        # Rede sem CNPJ no header: um cliente fixo (ver `cliente_matriz`). `fornecedor` segue a rede.
        nome_cliente, cnpj_cliente = cliente_matriz.nome, cliente_matriz.cnpj

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


def build_recebimentos_por_loja(
    *,
    import_id: str,
    order: Order,
    tenant_id: str,
    resolucao: ResolucaoCliente | None = None,
) -> list[RecebimentoRequest]:
    """Um recebimento por loja (ver `recebimento_por_loja`): mesmo `externalId`, CNPJ da filial em
    `cliente`, `indiceOriginal` em cada item. O chamador já decidiu dividir (`deve_dividir`)."""
    pedidos: list[RecebimentoRequest] = []
    fornecedor = order.header.customer_name or "(sem fornecedor)"
    for cnpj, indices in recebimento_por_loja.lojas(order).items():
        itens = [order.items[i] for i in indices]
        nome = next(
            (i.delivery_name.strip() for i in itens if (i.delivery_name or "").strip()), None
        )
        parcial = Order(
            header=order.header.model_copy(
                update={
                    "customer_cnpj": cnpj,
                    "customer_name": nome or order.header.customer_name,
                }
            ),
            items=itens,
            source_file=order.source_file,
        )
        req = build_recebimento_payload(
            import_id=import_id, order=parcial, tenant_id=tenant_id, resolucao=resolucao
        )
        req.fornecedor = fornecedor
        for item, indice in zip(req.itens, indices, strict=True):
            item.indiceOriginal = indice
        pedidos.append(req)
    return pedidos
