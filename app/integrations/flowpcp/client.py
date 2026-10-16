from __future__ import annotations

import hashlib
import json
from typing import Any

import httpx

from app.http.client import HttpError, OutboundClient
from app.http.policies import apply_post_policy, idempotent_post_policy
from app.integrations.flowpcp.catalogo_schema import (
    CatalogoReconciliacaoResponse,
    CatalogoRequest,
)
from app.integrations.flowpcp.clientes_schema import (
    ClientesReconciliacaoResponse,
    ClientesRequest,
)
from app.integrations.flowpcp.materiais_schema import MateriaisRequest, MateriaisResposta
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
_CATALOGO_PATH = "/api/portal-pedidos/catalogo"
_CLIENTES_PATH = "/api/portal-pedidos/clientes"
_MATERIAIS_PATH = "/api/portal-pedidos/materiais"
DEFAULT_TIMEOUT_SECONDS = 30.0
# Apply do catálogo: o Flow tem maxDuration 300 s e um apply por vez (409).
APPLY_TIMEOUT_SECONDS = 300.0


class FlowPCPClientError(Exception):
    def __init__(
        self, message: str, *, status_code: int | None = None, body: str | None = None
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.body = body


class FlowPCPApplyEmAndamentoError(FlowPCPClientError):
    """409 do /catalogo: o Flow já está gravando um catálogo deste tenant."""


def _apply_timeout() -> httpx.Timeout:
    """300 s é o tempo de LEITURA (maxDuration do Flow); conectar segue curto, senão
    um Flow inalcançável seguraria 3 x 300 s nas repetições de conexão."""
    return httpx.Timeout(
        APPLY_TIMEOUT_SECONDS, connect=DEFAULT_TIMEOUT_SECONDS, pool=DEFAULT_TIMEOUT_SECONDS
    )


def _idem_catalogo(request: CatalogoRequest) -> str:
    """Chave estável em retry e nova quando o conteúdo muda (só a contagem colidia:
    os mesmos 1.026 itens com composição nova). `origem` fica fora: muda a cada
    extração mesmo sem nada novo."""
    corpo = {k: v for k, v in request.model_dump(by_alias=True).items() if k != "origem"}
    serializado = json.dumps(corpo, sort_keys=True, default=str)
    digest = hashlib.sha256(serializado.encode()).hexdigest()[:16]
    return f"catalogo-{int(request.dryRun)}-{len(request.itens)}-{digest}"


def _idem_materiais(request: MateriaisRequest) -> str:
    """Mesmo esquema de `_idem_catalogo`: estável em retry, novo quando o conteúdo muda."""
    corpo = {k: v for k, v in request.model_dump(by_alias=True).items() if k != "origem"}
    serializado = json.dumps(corpo, sort_keys=True, default=str)
    digest = hashlib.sha256(serializado.encode()).hexdigest()[:16]
    return f"materiais-{int(request.dryRun)}-{len(request.itens)}-{digest}"


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

    def send_order(self, request: RecebimentoRequest, *, idempotency_key: str) -> dict[str, Any]:
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

    def send_catalogo(self, request: CatalogoRequest) -> CatalogoReconciliacaoResponse:
        body = request.model_dump(by_alias=True)
        idem = _idem_catalogo(request)
        # Apply grava e o Flow não deduplica pela chave: não repetir depois que a
        # conexão abriu, e esperar até o maxDuration do Flow. Dry-run segue como era.
        extras = (
            {}
            if request.dryRun
            else {"timeout": _apply_timeout(), "retry_policy": apply_post_policy()}
        )
        try:
            resp = self._client.post_json(_CATALOGO_PATH, json=body, idempotency_key=idem, **extras)
        except HttpError as exc:
            raise FlowPCPClientError(
                f"send_catalogo falhou: {exc}", status_code=exc.status_code, body=exc.body
            ) from exc
        if resp.status_code == 409:
            raise FlowPCPApplyEmAndamentoError(
                "o Flow já está gravando um catálogo deste ambiente",
                status_code=409,
                body=(resp.text or "")[:500],
            )
        if not resp.is_success:
            raise FlowPCPClientError(
                f"catalogo status {resp.status_code}",
                status_code=resp.status_code,
                body=(resp.text or "")[:500],
            )
        return CatalogoReconciliacaoResponse.model_validate(resp.json())

    def send_materiais(self, request: MateriaisRequest) -> MateriaisResposta:
        body = request.model_dump(by_alias=True)
        idem = _idem_materiais(request)
        extras = (
            {}
            if request.dryRun
            else {"timeout": _apply_timeout(), "retry_policy": apply_post_policy()}
        )
        try:
            resp = self._client.post_json(
                _MATERIAIS_PATH, json=body, idempotency_key=idem, **extras
            )
        except HttpError as exc:
            raise FlowPCPClientError(
                f"send_materiais falhou: {exc}", status_code=exc.status_code, body=exc.body
            ) from exc
        if resp.status_code == 409:
            raise FlowPCPApplyEmAndamentoError(
                "o Flow já está gravando materiais deste ambiente",
                status_code=409,
                body=(resp.text or "")[:500],
            )
        if not resp.is_success:
            raise FlowPCPClientError(
                f"materiais status {resp.status_code}",
                status_code=resp.status_code,
                body=(resp.text or "")[:500],
            )
        return MateriaisResposta.model_validate(resp.json())

    def send_clientes(self, request: ClientesRequest) -> ClientesReconciliacaoResponse:
        body = request.model_dump(by_alias=True)
        # I5: key inclui hash do conteúdo — estável em retry, único quando muda
        # (a contagem sozinha colidiria: 617 clientes seguem 617 com nomes corrigidos).
        payload_sig = "|".join(
            sorted(f"{i.cnpj}:{i.nome}:{i.grupoCodigo or ''}" for i in request.itens)
        )
        digest = hashlib.sha256(payload_sig.encode("utf-8")).hexdigest()[:16]
        idem = f"clientes-{int(request.dryRun)}-{digest}"
        try:
            resp = self._client.post_json(_CLIENTES_PATH, json=body, idempotency_key=idem)
        except HttpError as exc:
            raise FlowPCPClientError(
                f"send_clientes falhou: {exc}", status_code=exc.status_code, body=exc.body
            ) from exc
        if not resp.is_success:
            raise FlowPCPClientError(
                f"clientes status {resp.status_code}",
                status_code=resp.status_code,
                body=(resp.text or "")[:500],
            )
        return ClientesReconciliacaoResponse.model_validate(resp.json())

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
