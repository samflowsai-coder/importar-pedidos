"""Gestor de Produção HTTP client.

⚠️  PLACEHOLDER WIRE FORMAT — see `schema.py`. Endpoint, auth, and dates
are best-guess. When real spec arrives, this file likely needs only:
    - URL path constant (`_CREATE_ORDER_PATH`)
    - Auth header style (currently `Authorization: Bearer ...`)
    - 2xx interpretation (currently treats anything <300 as success)

Built on `app.http.OutboundClient` so we get retry, trace_id propagation,
and structured logs for free. The retry policy is `idempotent_post_policy`
because the caller is required to pass an `idempotency_key` (the outbox
row's UUID).
"""
from __future__ import annotations

import os
from typing import Any

from pydantic import ValidationError

from app.http.client import HttpError, OutboundClient
from app.http.policies import idempotent_post_policy
from app.integrations.gestor.schema import (
    GestorOrderRequest,
    GestorOrderResponse,
)
from app.utils.logger import logger

GESTOR_TARGET_NAME = "gestor"  # outbox.target identifier

# PLACEHOLDER — confirm endpoint path with real spec
_CREATE_ORDER_PATH = "/v1/orders"

DEFAULT_TIMEOUT_SECONDS = 30.0


class GestorClientError(Exception):
    """Final failure after retries (or non-retryable response)."""

    def __init__(
        self,
        message: str,
        *,
        status_code: int | None = None,
        body: str | None = None,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.body = body


def _env_or(name: str, default: str | None = None) -> str | None:
    val = os.environ.get(name)
    return val if val else default


class GestorClient:
    """Thin wrapper around OutboundClient with Gestor-specific knowledge."""

    def __init__(
        self,
        *,
        base_url: str | None = None,
        api_key: str | None = None,
        timeout: float = DEFAULT_TIMEOUT_SECONDS,
        outbound: OutboundClient | None = None,
    ) -> None:
        self._base_url = base_url or _env_or("GESTOR_BASE_URL", "")
        self._api_key = api_key or _env_or("GESTOR_API_KEY", "")
        if outbound is None:
            if not self._base_url:
                raise GestorClientError(
                    "GESTOR_BASE_URL not set and no `outbound` injected"
                )
            outbound = OutboundClient(
                base_url=self._base_url,
                timeout=timeout,
                retry_policy=idempotent_post_policy(),
                default_headers={"Content-Type": "application/json"},
            )
        self._client = outbound

    def close(self) -> None:
        self._client.close()

    def create_order(
        self,
        request: GestorOrderRequest,
        *,
        idempotency_key: str,
    ) -> GestorOrderResponse:
        """POST /v1/orders. Raises GestorClientError on any non-2xx after retries."""
        if not self._api_key:
            raise GestorClientError("GESTOR_API_KEY not set")

        body = request.model_dump(exclude_none=False)
        try:
            response = self._client.post_json(
                _CREATE_ORDER_PATH,
                json=body,
                idempotency_key=idempotency_key,
                headers={"Authorization": f"Bearer {self._api_key}"},
            )
        except HttpError as exc:
            raise GestorClientError(
                f"Gestor HTTP error: {exc}",
                status_code=exc.status_code,
                body=exc.body,
            ) from exc
        except Exception as exc:  # noqa: BLE001 — connection/timeouts after retries
            raise GestorClientError(
                f"Gestor unreachable: {type(exc).__name__}: {exc}"
            ) from exc

        if not response.is_success:
            preview = (response.text or "")[:500]
            logger.error(
                f"gestor create_order status={response.status_code} body={preview}"
            )
            raise GestorClientError(
                f"Gestor returned status {response.status_code}",
                status_code=response.status_code,
                body=preview,
            )

        try:
            data: dict[str, Any] = response.json()
        except ValueError as exc:
            raise GestorClientError(
                f"Gestor returned non-JSON: {response.text[:500]}"
            ) from exc

        try:
            return GestorOrderResponse.model_validate(data)
        except ValidationError as exc:
            # PLACEHOLDER spec mismatch — most common failure once real spec arrives
            raise GestorClientError(
                f"Gestor response failed schema validation: {exc.errors()[:3]}"
            ) from exc


__all__ = [
    "GESTOR_TARGET_NAME",
    "GestorClient",
    "GestorClientError",
]
