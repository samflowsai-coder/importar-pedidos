from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any

import httpx

from app.http.client import HttpError, OutboundClient
from app.http.policies import apply_post_policy, idempotent_post_policy
from app.integrations.flowpcp.cadastro_schema import PendentesResposta
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
_CADASTROS_PENDENTES_PATH = "/api/portal-pedidos/cadastros/pendentes"
DEFAULT_TIMEOUT_SECONDS = 30.0
# Apply do catálogo: o Flow tem maxDuration 300 s e um apply por vez (409).
APPLY_TIMEOUT_SECONDS = 300.0
_ORDEM_ACOES = ("criar", "atualizar", "inativar")


@dataclass(frozen=True)
class RespostaResultado:
    status_http: int | None
    corpo: dict | None
    retry_after: int | None


def _retry_after(resp: httpx.Response) -> int | None:
    bruto = (resp.headers.get("Retry-After") or "").strip()
    if not (bruto.isascii() and bruto.isdecimal()) or int(bruto) < 1:
        return None
    return min(int(bruto), 3600)


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


def _idem(prefixo: str, request) -> str:
    """Chave estável em retry e nova quando o conteúdo muda (só a contagem colidia:
    os mesmos 1.026 itens com composição nova). `origem` fica fora: muda a cada
    extração mesmo sem nada novo."""
    corpo = {k: v for k, v in request.model_dump(by_alias=True).items() if k != "origem"}
    serializado = json.dumps(corpo, sort_keys=True, default=str)
    digest = hashlib.sha256(serializado.encode()).hexdigest()[:16]
    return f"{prefixo}-{int(request.dryRun)}-{len(request.itens)}-{digest}"


def _idem_catalogo(request: CatalogoRequest) -> str:
    return _idem("catalogo", request)


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

    def list_cadastros_pendentes(
        self, limite: int = 20, *, acoes: tuple[str, ...] | None = None, simular: bool = False
    ) -> PendentesResposta:
        """Fila de cadastros aprovados. `simular=True` lê sem reservar (contrato §2);
        sem ele, todo GET reserva os itens por 10 minutos."""
        params: dict[str, Any] = {"limite": limite}
        if simular:
            params["simular"] = "1"
        if acoes:
            pedidas = set(acoes)
            if not pedidas <= set(_ORDEM_ACOES):
                raise ValueError("acoes com valor fora de criar/atualizar/inativar")
            params["acoes"] = ",".join(a for a in _ORDEM_ACOES if a in pedidas)
        try:
            resp = self._client.get(_CADASTROS_PENDENTES_PATH, params=params)
        except HttpError as exc:
            raise FlowPCPClientError(
                f"list_cadastros_pendentes falhou: {exc}",
                status_code=exc.status_code,
                body=exc.body,
            ) from exc
        if not resp.is_success:
            raise FlowPCPClientError(
                f"cadastros pendentes status {resp.status_code}",
                status_code=resp.status_code,
                body=(resp.text or "")[:500],
            )
        return PendentesResposta.model_validate(resp.json())

    def post_cadastro_resultado(self, cadastro_id: str, corpo: dict[str, Any]) -> RespostaResultado:
        """Resultado de um cadastro. Nunca levanta por status HTTP: quem decide é
        `cadastro_resultado.ler_resposta`. Transporte falhou = `status_http=None`.
        Idempotente no Flow por (cadastroId, versao)."""
        path = f"/api/portal-pedidos/cadastros/{cadastro_id}/gravado"
        try:
            resp = self._client.post_json(
                path,
                json=corpo,
                idempotency_key=(
                    f"cadastro-{cadastro_id}-{corpo.get('versao')}-{corpo.get('resultado')}"
                ),
                retry_policy=idempotent_post_policy(),
            )
        except (httpx.HTTPError, OSError):
            # timeout/rede: resultado incerto, o livro repete. Bug de código sobe.
            return RespostaResultado(None, None, None)
        try:
            dados = resp.json()
        except ValueError:
            dados = None
        return RespostaResultado(
            resp.status_code, dados if isinstance(dados, dict) else None, _retry_after(resp)
        )

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
        return self._post_relatorio(
            _CATALOGO_PATH,
            "catalogo",
            request,
            CatalogoReconciliacaoResponse,
            em_andamento="o Flow já está gravando um catálogo deste ambiente",
        )

    def send_materiais(self, request: MateriaisRequest) -> MateriaisResposta:
        return self._post_relatorio(
            _MATERIAIS_PATH,
            "materiais",
            request,
            MateriaisResposta,
            em_andamento="o Flow já está gravando materiais deste ambiente",
        )

    def _post_relatorio(self, path, nome, request, modelo, *, em_andamento: str):
        """POST de carga com relatório (`dryRun`). Apply grava e o Flow não deduplica
        pela chave: não repetir depois que a conexão abriu, e esperar até o maxDuration
        do Flow. Dry-run segue a política padrão."""
        body = request.model_dump(by_alias=True)
        extras = (
            {}
            if request.dryRun
            else {"timeout": _apply_timeout(), "retry_policy": apply_post_policy()}
        )
        try:
            resp = self._client.post_json(
                path, json=body, idempotency_key=_idem(nome, request), **extras
            )
        except HttpError as exc:
            raise FlowPCPClientError(
                f"send_{nome} falhou: {exc}", status_code=exc.status_code, body=exc.body
            ) from exc
        if resp.status_code == 409:
            raise FlowPCPApplyEmAndamentoError(
                em_andamento, status_code=409, body=(resp.text or "")[:500]
            )
        if not resp.is_success:
            raise FlowPCPClientError(
                f"{nome} status {resp.status_code}",
                status_code=resp.status_code,
                body=(resp.text or "")[:500],
            )
        return modelo.model_validate(resp.json())

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
