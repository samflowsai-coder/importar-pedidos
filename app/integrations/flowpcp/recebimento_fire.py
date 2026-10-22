"""Pedido que só existe no Fire → payload do `/recebimento` (tools/flow_enviar_pedidos_fire.py).

Pedido aberto do Fire que nunca passou pelo Portal não tem import nem snapshot: o
payload sai do `PedidoErp` que a vigia do SP1 já lê (`erp_eventos/leitura.py`), pelo
mesmo `build_recebimento_payload` do envio normal. Só muda o que é de origem
(`externalId` derivado do código do pedido no Fire, `origem.parserUsado`).
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping
from datetime import date

from app.erp_eventos.modelo import PedidoErp, esta_aberto
from app.integrations.flowpcp.mapper import build_recebimento_payload
from app.integrations.flowpcp.schema import OrigemRecebimento, RecebimentoRequest
from app.models.order import Order, OrderHeader, OrderItem

# O vínculo do Flow só liga o envio a um pedido do ERP quando `criado_em` do envio cai a
# até 90 dias da DATA_PEDIDO (erp-eventos/processar.ts). Margem de 5 dias pro ciclo do SP1.
JANELA_VINCULO_DIAS = 85
# Teto do contrato `pedido.recebimento.v1`.
MAX_ITENS = 500
MAX_PEDIDO_NUMERO = 64
_STATUS_ENVIAVEL = "PEDIDO"
_NAMESPACE = uuid.UUID("6f1d2c9e-6a43-5f7e-9b0c-2f8a4d1e7c55")


def external_id_do_fire(erp_codigo: int, *, slug: str) -> str:
    """Estável por (empresa, pedido): reenviar devolve `alreadyProcessed`, nunca duplica."""
    return str(uuid.uuid5(_NAMESPACE, f"{slug}:fire:{erp_codigo}"))


def _br(d: date | None) -> str | None:
    return d.strftime("%d/%m/%Y") if d else None


def _centesimos(q) -> bool:
    return q * 100 == (q * 100).to_integral_value()


def recusa_local(p: PedidoErp, *, hoje: date) -> str | None:
    """Motivo pelo qual o pedido NÃO deve ser enviado, ou None. Não consulta ninguém."""
    if p.status.strip().upper() != _STATUS_ENVIAVEL or not esta_aberto(p.status):
        return f"status {p.status!r} (só {_STATUS_ENVIAVEL} é enviado)"
    numero = (p.pedido_cliente or "").strip()
    if not numero or len(numero) > MAX_PEDIDO_NUMERO:
        return "pedido do cliente vazio ou acima de 64 caracteres"
    if not (p.cliente_real_cnpj or p.cliente_erp_cnpj):
        return "cliente sem CNPJ de 14 dígitos (o vínculo do Flow casa o cliente por CNPJ)"
    if p.data_pedido is None:
        return "pedido sem data do pedido"
    if (hoje - p.data_pedido).days > JANELA_VINCULO_DIAS:
        return (
            f"fora da janela de vínculo do Flow ({(hoje - p.data_pedido).days} dias; "
            f"limite {JANELA_VINCULO_DIAS})"
        )
    if not p.itens:
        return "pedido sem itens"
    if len(p.itens) > MAX_ITENS:
        return f"mais de {MAX_ITENS} itens (limite do contrato)"
    for i in p.itens:
        if i.qtd <= 0 or not _centesimos(i.qtd):
            return f"item {i.erp_item_codigo}: quantidade {i.qtd} inválida para o Flow"
        if not i.descricao.strip():
            return f"item {i.erp_item_codigo}: sem descrição"
        if i.qtd_faturada > 0:
            return f"item {i.erp_item_codigo}: já faturado em parte (o saldo não é o pedido)"
    return None


def montar_recebimento(
    p: PedidoErp,
    *,
    razao_social: str,
    slug: str,
    codigos_flow: Mapping[int, str] | None = None,
) -> RecebimentoRequest:
    """`codigos_flow`: erp_item_codigo → `produtos.codigo` no Flow (o relatório do vínculo
    traz; inclui códigos legados como KIT-2972). Sem ele, vai o SEQ do Fire, que é o
    `codigo` que o catálogo grava."""
    codigos = codigos_flow or {}
    itens = sorted(p.itens, key=lambda i: i.erp_item_codigo)
    order = Order(
        header=OrderHeader(
            order_number=(p.pedido_cliente or "").strip(),
            issue_date=_br(p.data_pedido),
            customer_name=razao_social,
            customer_cnpj=p.cliente_real_cnpj or p.cliente_erp_cnpj,
        ),
        items=[
            OrderItem(
                description=i.descricao,
                product_code=codigos.get(i.erp_item_codigo)
                or (str(i.cod_produto) if i.cod_produto is not None else None),
                quantity=float(i.qtd),
                unit_price=float(i.preco_unitario) if i.preco_unitario is not None else None,
                delivery_date=_br(i.dt_entrega_item or p.dt_entrega),
            )
            for i in itens
        ],
        source_file=f"fire:{p.erp_codigo}",
    )
    req = build_recebimento_payload(
        import_id=external_id_do_fire(p.erp_codigo, slug=slug), order=order, tenant_id=""
    )
    return req.model_copy(
        update={
            "origem": OrigemRecebimento(
                importadorVersao=req.origem.importadorVersao,
                arquivoOriginal=f"fire:{p.erp_codigo}",
                parserUsado="fire_direto",
                confiancaParser="alta",
            )
        }
    )
