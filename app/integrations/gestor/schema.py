"""Mock spec of the Gestor de Produção HTTP API.

⚠️  PLACEHOLDER — spec not yet provided. Substitute when real one arrives.

Conventions assumed (subject to confirmation):
    - REST/JSON, idempotency-key header for replay safety.
    - Bearer auth via `GESTOR_API_KEY`.
    - Dates in ISO 8601 (YYYY-MM-DD). Internal Order uses BR format
      (DD/MM/YYYY); the mapper converts.
    - `external_id` = portal `import_id` (UUID), so Gestor can echo back
      and we correlate via webhooks (Phase 4).
    - Numbers as JSON numbers (no string-encoded decimals).

Endpoints assumed:
    POST  {GESTOR_BASE_URL}/v1/orders        — create production order
    GET   {GESTOR_BASE_URL}/v1/orders/{id}   — read status (Phase 5+)

If the real spec uses different field names, ENVELOPE shape, or auth
method, edit only this file + `mapper.py`. The `client.py`, outbox
plumbing, and route do not need changes.
"""
from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

# ── PLACEHOLDER SHAPES ────────────────────────────────────────────────────
# The pydantic models below mirror the assumed wire format. They exist for
# two reasons:
#   1. Validate the payload we send (catches mapper bugs early).
#   2. Validate the response we get (catches API contract drift).
# When real spec arrives: rename fields here, the rest of the codebase only
# refers to these models.


class GestorDelivery(BaseModel):
    """PLACEHOLDER — assumed delivery sub-document inside an item."""
    model_config = ConfigDict(extra="ignore")

    name: str | None = None
    cnpj: str | None = None
    ean: str | None = None  # store EAN (ex: Sam's Club GRADE)


class GestorItem(BaseModel):
    """PLACEHOLDER — assumed shape of one production-order line item."""
    model_config = ConfigDict(extra="ignore")

    external_item_id: str = Field(
        description="Stable id within the portal's order (index or product_code)"
    )
    description: str | None = None
    product_code: str | None = None
    ean: str | None = None
    quantity: float
    unit_price: float | None = None
    total_price: float | None = None
    delivery_date: str | None = None  # ISO YYYY-MM-DD
    delivery: GestorDelivery | None = None
    obs: str | None = None


class GestorCustomer(BaseModel):
    """PLACEHOLDER — assumed customer sub-document on the order header."""
    model_config = ConfigDict(extra="ignore")

    name: str | None = None
    cnpj: str | None = None


class GestorOrderRequest(BaseModel):
    """PLACEHOLDER — body of POST /v1/orders.

    Field renames likely; structural layout is a reasonable best-guess.
    """
    model_config = ConfigDict(extra="ignore")

    external_id: str = Field(description="Portal import_id (UUID)")
    supplier_order_number: str | None = Field(
        default=None, description="Order number from the retailer's PDF/XLS"
    )
    customer: GestorCustomer = Field(default_factory=GestorCustomer)
    issue_date: str | None = None  # ISO YYYY-MM-DD
    items: list[GestorItem] = Field(default_factory=list)
    metadata: dict = Field(default_factory=dict)


class GestorOrderResponse(BaseModel):
    """PLACEHOLDER — response of POST /v1/orders.

    `id` is what we store on `imports.gestor_order_id` to correlate webhooks.
    """
    model_config = ConfigDict(extra="ignore")

    id: str = Field(description="Gestor's internal order id")
    external_id: str | None = None
    status: str | None = None
    received_at: str | None = None


__all__ = [
    "GestorCustomer",
    "GestorDelivery",
    "GestorItem",
    "GestorOrderRequest",
    "GestorOrderResponse",
]
