"""Gestor de Produção integration (outbound).

Phase 3 wiring. Spec is currently MOCK — see `schema.py`. When the real
spec arrives, swap `schema.py` (request/response shapes) and re-tune
`mapper.py`. Public surface is stable:

    from app.integrations.gestor import (
        GestorClient, build_gestor_payload, GESTOR_TARGET_NAME,
    )

The flow `/api/imported/{id}/post-to-gestor` lives in `app.web.server`
and orchestrates: build payload → enqueue outbox → drain inline.
"""
from app.integrations.gestor.client import (
    GESTOR_TARGET_NAME,
    GestorClient,
    GestorClientError,
)
from app.integrations.gestor.mapper import build_gestor_payload
from app.integrations.gestor.schema import (
    GestorOrderRequest,
    GestorOrderResponse,
)

__all__ = [
    "GESTOR_TARGET_NAME",
    "GestorClient",
    "GestorClientError",
    "GestorOrderRequest",
    "GestorOrderResponse",
    "build_gestor_payload",
]
