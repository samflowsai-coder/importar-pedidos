"""Mock spec of the Gestor de Produção webhook payload.

⚠️  PLACEHOLDER — spec not yet provided. When real one arrives, edit only
this file + the event-type → LifecycleEvent mapping in `app/web/webhooks.py`.

Conventions assumed (subject to confirmation):

    Headers:
        X-Signature: sha256=<hex>      — HMAC over `f"{timestamp}.{body}"`
        X-Timestamp: <unix seconds>    — replay protection (5min skew)
        X-Event-Id: <uuid>             — idempotency key
        Content-Type: application/json

    Body (POST /api/webhooks/gestor):
        {
            "event_id": "<uuid>",          — same as X-Event-Id (defensive copy)
            "event_type": "production_update" | "production_completed" |
                          "production_cancelled",
            "external_id": "<portal import_id>",  — correlation
            "gestor_order_id": "<gestor uuid>",   — echo of POST /v1/orders response
            "apontae_order_id": "<apontae uuid>", — first-seen on initial event
            "occurred_at": "<ISO 8601>",   — when the event happened upstream
            "payload": { ... }              — partial update, opaque to Portal
        }

    Response (200 OK):
        {"received": true, "import_id": "..."}

    Response (4xx): error details, unique 401/403/422 semantics
"""
from __future__ import annotations

from enum import Enum

from pydantic import BaseModel, ConfigDict, Field


class GestorWebhookEventType(str, Enum):
    """PLACEHOLDER — assumed event types from Gestor.

    Map to `app.state.LifecycleEvent` in `app/web/webhooks.py`. Adding a
    new event type here without wiring it there raises in the route.
    """
    PRODUCTION_UPDATE = "production_update"
    PRODUCTION_COMPLETED = "production_completed"
    PRODUCTION_CANCELLED = "production_cancelled"


class GestorWebhookEvent(BaseModel):
    """PLACEHOLDER — body of POST /api/webhooks/gestor.

    `extra="ignore"`: forward-compat with provider adding fields. Removal
    or rename surfaces as a 422 ValidationError, which is what we want.
    """
    model_config = ConfigDict(extra="ignore")

    event_id: str = Field(description="UUID for idempotency dedup")
    event_type: GestorWebhookEventType
    external_id: str | None = Field(
        default=None,
        description="Portal import_id (UUID) — correlation. Falls back to "
                    "lookup by gestor_order_id if absent.",
    )
    gestor_order_id: str | None = None
    apontae_order_id: str | None = None
    occurred_at: str | None = None  # ISO 8601 from Gestor
    payload: dict = Field(default_factory=dict)


__all__ = ["GestorWebhookEvent", "GestorWebhookEventType"]
