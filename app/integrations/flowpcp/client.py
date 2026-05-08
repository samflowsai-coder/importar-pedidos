"""FlowPCP HTTP client — POST /api/portal-pedidos/produtos/sync.

Built on `app.http.OutboundClient` so retry, trace propagation, redacted
logs come for free.
"""
from __future__ import annotations

from datetime import UTC, datetime

from pydantic import ValidationError

from app.http.client import HttpError, OutboundClient
from app.http.policies import idempotent_post_policy
from app.integrations.flowpcp.schema import (
    FlowPCPSyncRequest,
    FlowPCPSyncResponse,
)
from app.utils.logger import logger

FLOWPCP_TARGET_NAME = "flowpcp"

SYNC_PATH = "/api/portal-pedidos/produtos/sync"
HEALTH_PATH = "/api/portal-pedidos/health"

DEFAULT_TIMEOUT_SECONDS = 30.0


class FlowPCPClientError(Exception):
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


class FlowPCPClient:
    def __init__(
        self,
        *,
        base_url: str,
        api_key: str,
        tenant_id: str,
        timeout: float = DEFAULT_TIMEOUT_SECONDS,
        outbound: OutboundClient | None = None,
    ) -> None:
        if not base_url:
            raise FlowPCPClientError("base_url required")
        if not api_key:
            raise FlowPCPClientError("api_key required")
        if not tenant_id:
            raise FlowPCPClientError("tenant_id required")
        self._base_url = base_url
        self._api_key = api_key
        self._tenant_id = tenant_id
        if outbound is None:
            outbound = OutboundClient(
                base_url=base_url,
                timeout=timeout,
                retry_policy=idempotent_post_policy(),
                default_headers={"Content-Type": "application/json"},
            )
        self._client = outbound

    def close(self) -> None:
        self._client.close()

    def sync_products(
        self,
        *,
        produtos: list[dict],
        componentes: list[dict],
        sync_id: str,
        trace_id: str | None,
        delta_kind: str = "incremental",
    ) -> FlowPCPSyncResponse:
        body = FlowPCPSyncRequest(
            tenant_id=self._tenant_id,
            sync_id=sync_id,
            generated_at=datetime.now(UTC).isoformat(timespec="seconds").replace(
                "+00:00", "Z"
            ),
            delta_kind=delta_kind,
            produtos=produtos,
            componentes=componentes,
        ).model_dump(mode="json")

        headers = {"Authorization": f"Bearer {self._api_key}"}
        if trace_id:
            headers["X-Trace-Id"] = trace_id

        try:
            response = self._client.post_json(
                SYNC_PATH,
                json=body,
                idempotency_key=sync_id,
                headers=headers,
            )
        except HttpError as exc:
            raise FlowPCPClientError(
                f"flowpcp HTTP error: {exc}",
                status_code=exc.status_code,
                body=exc.body,
            ) from exc
        except Exception as exc:  # noqa: BLE001
            raise FlowPCPClientError(
                f"flowpcp unreachable: {type(exc).__name__}: {exc}"
            ) from exc

        if not response.is_success:
            preview = (response.text or "")[:500]
            logger.error(
                f"flowpcp sync_products status={response.status_code} body={preview}"
            )
            raise FlowPCPClientError(
                f"flowpcp returned status {response.status_code}",
                status_code=response.status_code,
                body=preview,
            )

        try:
            data = response.json()
        except ValueError as exc:
            raise FlowPCPClientError(
                f"flowpcp non-JSON: {response.text[:500]}"
            ) from exc

        try:
            return FlowPCPSyncResponse.model_validate(data)
        except ValidationError as exc:
            raise FlowPCPClientError(
                f"flowpcp response schema mismatch: {exc.errors()[:3]}"
            ) from exc

    def health(self) -> bool:
        headers = {"Authorization": f"Bearer {self._api_key}"}
        try:
            response = self._client.get(HEALTH_PATH, headers=headers)
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"flowpcp health failed: {exc}")
            return False
        return response.is_success


__all__ = [
    "FlowPCPClient",
    "FlowPCPClientError",
    "FLOWPCP_TARGET_NAME",
    "SYNC_PATH",
    "HEALTH_PATH",
]
