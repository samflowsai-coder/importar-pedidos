"""
Maps our Order/ERPRow models to Fire Sistemas Firebird table rows.

Schema + data patterns verified against MM_AMERICANENSE 2026-04-21 backup
(Firebird 2.5 → restored to Firebird 5, ODS 13.1 with WIN1252 charset).

Key design decisions (data-driven):
- STATUS='PEDIDO' for newly imported orders — matches production convention
  (other statuses: 'EM ANÁLISE', 'FATURADO', 'CANCELADO').
- DOCUMENTO left NULL — the retailer's reference goes to PEDIDO_CLIENTE only.
- CLINAOCAD path abandoned — production has zero rows using it; every CAB_VENDAS
  has a CLIENTE FK. If client CNPJ can't be resolved in CADASTRO we skip and
  surface an error rather than insert an orphan record.
- CODPRODUTO may be NULL when product not found; item still inserted with description.
- Dates parsed from DD/MM/YYYY (already normalized by OrderNormalizer).
"""

from __future__ import annotations

import re
from datetime import date, datetime
from typing import Optional

from app.models.order import ERPRow, Order


def _digits_only(value: str | None) -> str:
    if not value:
        return ""
    return re.sub(r"\D", "", value)


def _parse_date(value: str | None) -> date | None:
    if not value:
        return None
    for fmt in ("%d/%m/%Y", "%Y-%m-%d", "%d/%m/%y"):
        try:
            return datetime.strptime(value.strip(), fmt).date()
        except ValueError:
            continue
    return None


class FireSistemasMapper:
    """Maps Order model to Fire Sistemas CAB_VENDAS + CORPO_VENDAS rows."""

    EMPRESA_CODIGO = 1  # default company code; override via FB_CODEMPRESA env var
    STATUS_INICIAL = "PEDIDO"
    USUARIO_SISTEMA = "IMPORTADOR"

    def order_to_cabvendas(
        self,
        order: Order,
        header_pk: int,
        client_id: int,
    ) -> tuple:
        """Returns positional tuple for INSERT_CAB_VENDAS parameters.

        client_id is required (NOT Optional) — callers must resolve the CNPJ
        before reaching this point.
        """
        import os
        empresa = int(os.environ.get("FB_CODEMPRESA", self.EMPRESA_CODIGO))

        pedido_cliente = (order.header.order_number or "")[:20] or None
        data_pedido = _parse_date(order.header.issue_date) or date.today()

        return (
            header_pk,              # CODIGO
            empresa,                # CODEMPRESA
            data_pedido,            # DATA_PEDIDO
            client_id,              # CLIENTE
            self.STATUS_INICIAL,    # STATUS = 'PEDIDO'
            pedido_cliente,         # PEDIDO_CLIENTE (retailer ref)
            None,                   # OBS
            None,                   # DT_ENTREGA (header; items carry DT_ENTREGA_ITEM)
            self.USUARIO_SISTEMA,   # ULT_INS_USER
        )

    def item_to_corpovendas(
        self,
        item: ERPRow,
        item_pk: int,
        header_pk: int,
        product_seq: Optional[int],
    ) -> tuple:
        """Returns positional tuple for INSERT_CORPO_VENDAS parameters."""
        qty = item.quantidade or 0.0
        unit_price = item.preco_unitario or 0.0
        total = item.valor_total if item.valor_total is not None else round(qty * unit_price, 4)
        desc = (item.descricao or "")[:100]
        delivery = _parse_date(item.data_entrega)

        return (
            item_pk,            # CODIGO
            header_pk,          # CODVENDA
            product_seq,        # CODPRODUTO (FK or NULL if not found)
            desc,               # DESCRICAO
            qty,                # QTD
            unit_price,         # PRECO_UNITARIO
            total,              # TOTAL
            "UN",               # UNID
            delivery,           # DT_ENTREGA_ITEM
        )
