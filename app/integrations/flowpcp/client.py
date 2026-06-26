from __future__ import annotations

from typing import Any

from app.http.client import HttpError, OutboundClient
from app.http.policies import idempotent_post_policy
from app.integrations.flowpcp.schema import (
    ConfirmarReconciliacaoRequest,
    DecisoesResponse,
    RecebimentoRequest,
)
from app.utils.logger import logger

FLOWPCP_TARGET_NAME = "flowpcp"  # outbox.target identifier
RECEBIMENTO_PATH = "/api/portal-pedidos/recebimento"
_RECEBIMENTO_PATH = RECEBIMENTO_PATH
_DECISOES_PATH = "/api/portal-pedidos/decisoes"
DEFAULT_TIMEOUT_SECONDS = 30.0


class FlowPCPClientError(Exception):
    def __init__(
        self, message: str, *, status_code: int | None = None, body: str | None = None
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.body = body


class FlowPCPClient:
    def __init__(
        self,
        *,
        base_url: str,
        service_token: str,
        tenant_id: str,
        timeout: float = DEFAULT_TIMEOUT_SECONDS,
        outbound: OutboundClient | None = None,
    ) -> None:
        self._service_token = service_token
        self._tenant_id = tenant_id
        if outbound is None:
            outbound = OutboundClient(
                base_url=base_url,
                timeout=timeout,
                retry_policy=idempotent_post_policy(),
                default_headers={
                    "X-Service-Token": service_token,
                    "X-Tenant-Id": tenant_id,
                    "Content-Type": "application/json",
                },
            )
        self._client = outbound

    def close(self) -> None:
        self._client.close()

    def send_order(
        self, request: RecebimentoRequest, *, idempotency_key: str
    ) -> dict[str, Any]:
        body = request.model_dump(by_alias=True, exclude_none=False)
        resp = self._post(_RECEBIMENTO_PATH, body, idempotency_key=idempotency_key)
        return resp.json()

    def list_decisoes(self, cursor: str | None = None, limit: int = 50) -> DecisoesResponse:
        params: dict[str, Any] = {"limit": limit}
        if cursor:
            params["cursor"] = cursor
        try:
            resp = self._client.get(_DECISOES_PATH, params=params)
        except HttpError as exc:
            raise FlowPCPClientError(
                f"list_decisoes falhou: {exc}", status_code=exc.status_code, body=exc.body
            ) from exc
        if not resp.is_success:
            raise FlowPCPClientError(
                f"decisoes status {resp.status_code}",
                status_code=resp.status_code,
                body=(resp.text or "")[:500],
            )
        return DecisoesResponse.model_validate(resp.json())

    def confirmar_reconciliacao(
        self, decisao_id: str, request: ConfirmarReconciliacaoRequest
    ) -> dict[str, Any]:
        path = f"{_DECISOES_PATH}/{decisao_id}/confirmar-reconciliacao"
        body = request.model_dump(mode="json", exclude_none=True)
        try:
            resp = self._client.post_json(
                path, json=body, idempotency_key=f"reconciliar-{decisao_id}-{body['acao']}"
            )
        except HttpError as exc:
            raise FlowPCPClientError(
                f"confirmar falhou: {exc}", status_code=exc.status_code, body=exc.body
            ) from exc
        if resp.status_code == 409:
            logger.warning(f"flowpcp confirmar 409 (ja_reconciliado) decisao={decisao_id}")
            return {"conflict": True, "details": resp.json()}
        if not resp.is_success:
            raise FlowPCPClientError(
                f"confirmar status {resp.status_code}",
                status_code=resp.status_code,
                body=(resp.text or "")[:500],
            )
        return resp.json()

    def _post(self, path: str, body: dict[str, Any], *, idempotency_key: str):
        try:
            resp = self._client.post_json(path, json=body, idempotency_key=idempotency_key)
        except HttpError as exc:
            raise FlowPCPClientError(
                f"POST {path} falhou: {exc}", status_code=exc.status_code, body=exc.body
            ) from exc
        if not resp.is_success:
            raise FlowPCPClientError(
                f"POST {path} status {resp.status_code}",
                status_code=resp.status_code,
                body=(resp.text or "")[:500],
            )
        return resp
