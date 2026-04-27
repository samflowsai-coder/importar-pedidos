"""Map portal `Order` → Gestor de Produção request payload.

⚠️  Coupled to PLACEHOLDER spec in `schema.py`. When real spec arrives,
adjust this module + `schema.py` together.

Date convention: portal stores `DD/MM/YYYY` strings (Brazilian PDFs).
Gestor (assumed) wants `YYYY-MM-DD`. We convert here. If parse fails we
pass the original string through and let the API reject it loudly — better
than silently sending a Jan/Feb swap.
"""
from __future__ import annotations

from datetime import datetime

from app.integrations.gestor.schema import (
    GestorCustomer,
    GestorDelivery,
    GestorItem,
    GestorOrderRequest,
)
from app.models.order import Order, OrderItem


def _to_iso_date(br_date: str | None) -> str | None:
    """Convert 'DD/MM/YYYY' → 'YYYY-MM-DD'. Return original on parse failure."""
    if not br_date:
        return None
    s = br_date.strip()
    for fmt in ("%d/%m/%Y", "%Y-%m-%d", "%d/%m/%y"):
        try:
            return datetime.strptime(s, fmt).strftime("%Y-%m-%d")
        except ValueError:
            continue
    return s  # let the API reject if truly unparseable


def _has_delivery_info(item: OrderItem) -> bool:
    return any((item.delivery_name, item.delivery_cnpj, item.delivery_ean))


def _map_item(index: int, item: OrderItem) -> GestorItem:
    delivery = (
        GestorDelivery(
            name=item.delivery_name,
            cnpj=item.delivery_cnpj,
            ean=item.delivery_ean,
        )
        if _has_delivery_info(item)
        else None
    )
    return GestorItem(
        external_item_id=item.product_code or str(index),
        description=item.description,
        product_code=item.product_code,
        ean=item.ean,
        quantity=float(item.quantity or 0),
        unit_price=float(item.unit_price) if item.unit_price is not None else None,
        total_price=float(item.total_price) if item.total_price is not None else None,
        delivery_date=_to_iso_date(item.delivery_date),
        delivery=delivery,
        obs=item.obs,
    )


def build_gestor_payload(
    *,
    import_id: str,
    order: Order,
    metadata: dict | None = None,
) -> GestorOrderRequest:
    """Build the request body for POST /v1/orders.

    `metadata` is forwarded as-is — useful for Portal-side correlation
    fields (`fire_codigo`, `trace_id`) that don't fit the structured schema.
    """
    return GestorOrderRequest(
        external_id=import_id,
        supplier_order_number=order.header.order_number,
        customer=GestorCustomer(
            name=order.header.customer_name,
            cnpj=order.header.customer_cnpj,
        ),
        issue_date=_to_iso_date(order.header.issue_date),
        items=[_map_item(i, it) for i, it in enumerate(order.items)],
        metadata=dict(metadata or {}),
    )


__all__ = ["build_gestor_payload"]
