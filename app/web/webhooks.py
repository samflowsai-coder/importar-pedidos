"""Inbound webhook handlers — currently only Gestor de Produção.

⚠️  Wire format is PLACEHOLDER. See `app/integrations/gestor/webhook_schema.py`.

Pipeline for every webhook:
    1. Read raw body (we need it for HMAC verification before parsing).
    2. Verify `X-Signature` + `X-Timestamp` headers against current/previous
       secrets. Reject 401/403 on failure (no parsing, no DB write).
    3. Parse JSON → pydantic model. 422 if shape wrong.
    4. Idempotency: `record_attempt(provider, event_id)`. If already seen,
       return the cached response — no re-processing.
    5. Resolve `import_id` from `external_id` (preferred) or
       `gestor_order_id` reverse lookup.
    6. Map event_type → LifecycleEvent and call `transition()`.
    7. Side effects: stamp `apontae_order_id` on first event that has it.
    8. `finalize(provider, event_id, status, body)` — caches the response.

The route never raises uncaught — all error paths produce structured JSON
so the provider's retry logic gets clean signal.
"""
from __future__ import annotations

import json
import os
from typing import Any

from fastapi import APIRouter, Header, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import ValidationError

from app.integrations.gestor.webhook_schema import (
    GestorWebhookEvent,
    GestorWebhookEventType,
)
from app.observability.metrics import webhook_received_total
from app.observability.trace import current_trace_id, with_trace_id
from app.persistence import idempotency_repo, repo
from app.security import (
    InvalidSignatureError,
    ReplayedRequestError,
    SignatureRequiredError,
    verify_hmac_request,
)
from app.state import (
    EventSource,
    InvalidTransitionError,
    LifecycleEvent,
    transition,
)
from app.utils.logger import logger

router = APIRouter(prefix="/api/webhooks", tags=["webhooks"])

GESTOR_PROVIDER = "gestor"

# PLACEHOLDER — confirm naming when real spec arrives.
# Map webhook event_type → app.state.LifecycleEvent. Adding a new type to
# `GestorWebhookEventType` without registering it here surfaces as KeyError
# below (test catches this).
_GESTOR_EVENT_MAP: dict[GestorWebhookEventType, LifecycleEvent] = {
    GestorWebhookEventType.PRODUCTION_UPDATE: LifecycleEvent.PRODUCTION_UPDATE,
    GestorWebhookEventType.PRODUCTION_COMPLETED: LifecycleEvent.PRODUCTION_COMPLETED,
    GestorWebhookEventType.PRODUCTION_CANCELLED: LifecycleEvent.PRODUCTION_CANCELLED,
}


def _gestor_secrets() -> list[str]:
    """Current + previous webhook secrets, in that priority order.

    Rotation pattern: deploy with both set. After confirming the new key
    works, drop the previous. Default `secrets=[]` causes the verifier to
    fail closed with SignatureRequiredError (clearer than passing through).
    """
    return [
        os.environ.get("WEBHOOK_SECRET_GESTOR", ""),
        os.environ.get("WEBHOOK_SECRET_GESTOR_PREVIOUS", ""),
    ]


def _resolve_import_id(event: GestorWebhookEvent) -> str | None:
    """Prefer `external_id` (echoed back by Gestor); fall back to
    reverse-lookup by `gestor_order_id`."""
    if event.external_id:
        return event.external_id
    if event.gestor_order_id:
        return repo.find_import_id_by_gestor(event.gestor_order_id)
    return None


def _build_payload(event: GestorWebhookEvent) -> dict[str, Any]:
    """Shape the event body to store in the lifecycle log. Defensive copy."""
    return {
        "webhook_event_id": event.event_id,
        "event_type": event.event_type.value,
        "occurred_at": event.occurred_at,
        "gestor_order_id": event.gestor_order_id,
        "apontae_order_id": event.apontae_order_id,
        "payload": event.payload,
    }


@router.post("/gestor")
async def gestor_webhook(
    request: Request,
    x_signature: str | None = Header(default=None, alias="X-Signature"),
    x_timestamp: str | None = Header(default=None, alias="X-Timestamp"),
) -> JSONResponse:
    """Receive a partial status update from the Gestor de Produção.

    Returns 200 on accept (including idempotent replays), 401 on missing
    signature, 403 on bad signature / replay, 422 on schema mismatch, 404
    on unresolvable correlation, 409 on invalid SM transition.
    """
    body = await request.body()

    # 1) Verify signature first — never touch DB / parse JSON without it.
    try:
        verify_hmac_request(
            body=body,
            signature_header=x_signature,
            timestamp_header=x_timestamp,
            secrets=_gestor_secrets(),
        )
    except SignatureRequiredError as exc:
        raise HTTPException(status_code=401, detail=str(exc)) from exc
    except (InvalidSignatureError, ReplayedRequestError) as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc

    # Count after HMAC passes — unauthenticated noise is not a webhook.
    webhook_received_total.labels(provider=GESTOR_PROVIDER).inc()

    # 2) Parse + validate
    try:
        raw = json.loads(body or b"{}")
    except json.JSONDecodeError as exc:
        raise HTTPException(status_code=422, detail=f"Invalid JSON: {exc}") from exc
    try:
        event = GestorWebhookEvent.model_validate(raw)
    except ValidationError as exc:
        raise HTTPException(
            status_code=422,
            detail=f"Webhook payload failed schema validation: {exc.errors()[:3]}",
        ) from exc

    # 3) Idempotency claim BEFORE any side effect
    cached = idempotency_repo.record_attempt(GESTOR_PROVIDER, event.event_id)
    if cached is not None:
        # Already seen. If we have a finalized response, replay it. If not
        # (concurrent in-flight), return 202 to ask the caller to retry.
        if cached.response_status is None:
            return JSONResponse(
                {"received": False, "reason": "in_flight_retry_later"},
                status_code=202,
            )
        try:
            cached_body = json.loads(cached.response_body) if cached.response_body else {}
        except json.JSONDecodeError:
            cached_body = {"received": True}
        logger.info(f"webhook gestor event_id={event.event_id} replay → cached")
        return JSONResponse(cached_body, status_code=cached.response_status)

    # 4) Resolve correlation
    import_id = _resolve_import_id(event)
    if import_id is None:
        body_out = {
            "received": False,
            "reason": "unknown_external_id_and_gestor_order_id",
        }
        idempotency_repo.finalize(
            GESTOR_PROVIDER, event.event_id, status=404, body=json.dumps(body_out),
        )
        raise HTTPException(status_code=404, detail=body_out["reason"])

    # 5) Drive the state machine, propagating Portal trace_id for correlation.
    #    `external_id` may have been forged or stale — verify the row exists.
    entry = repo.get_import(import_id)
    if entry is None:
        body_out = {
            "received": False,
            "reason": f"unknown_import_id:{import_id}",
        }
        idempotency_repo.finalize(
            GESTOR_PROVIDER, event.event_id, status=404, body=json.dumps(body_out),
        )
        raise HTTPException(status_code=404, detail=body_out["reason"])
    portal_trace = entry.get("trace_id")

    try:
        sm_event = _GESTOR_EVENT_MAP[event.event_type]
    except KeyError:
        body_out = {"received": False, "reason": f"unmapped_event_type:{event.event_type.value}"}
        idempotency_repo.finalize(
            GESTOR_PROVIDER, event.event_id,
            status=422, body=json.dumps(body_out), import_id=import_id,
        )
        raise HTTPException(status_code=422, detail=body_out["reason"])  # noqa: B904

    with with_trace_id(portal_trace):
        # Stamp apontae_order_id idempotently on every event that includes it.
        if event.apontae_order_id:
            repo.set_apontae_order_id(import_id, event.apontae_order_id)

        try:
            result = transition(
                import_id,
                sm_event,
                source=EventSource.GESTOR,
                payload=_build_payload(event),
                trace_id=current_trace_id(),
            )
        except InvalidTransitionError as exc:
            body_out = {"received": False, "reason": str(exc)}
            idempotency_repo.finalize(
                GESTOR_PROVIDER, event.event_id,
                status=409, body=json.dumps(body_out), import_id=import_id,
            )
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    body_out = {
        "received": True,
        "import_id": import_id,
        "production_status": result.production_status.value,
        "state_version": result.state_version,
    }
    idempotency_repo.finalize(
        GESTOR_PROVIDER, event.event_id,
        status=200, body=json.dumps(body_out), import_id=import_id,
    )
    return JSONResponse(body_out, status_code=200)
