from __future__ import annotations

import hashlib
import json
from typing import Any

import httpx

from app.http.client import HttpError, OutboundClient
from app.http.policies import apply_post_policy, idempotent_post_policy, read_only_policy
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
from app.integrations.flowpcp.precos_schema import (
    AprovacaoRespostaV1,
    ConfirmacaoRespostaV1,
    DecisoesPaginaV1,
    TabelaPaginaV1,
)
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
_PRECOS_TABELA_PATH = "/api/portal-pedidos/precos/tabela"
_PRECOS_APROVACOES_PATH = "/api/portal-pedidos/precos/aprovacoes"
_PRECOS_DECISOES_PATH = "/api/portal-pedidos/precos/decisoes"
_PRECOS_CONFIRMAR_PATH = "/api/portal-pedidos/precos/decisoes/confirmar"
DEFAULT_TIMEOUT_SECONDS = 30.0
# Apply do catálogo: o Flow tem maxDuration 300 s e um apply por vez (409).
APPLY_TIMEOUT_SECONDS = 300.0


class FlowPCPClientError(Exception):
    """`error`/`details`: o corpo de erro do Flow (`{error, details?}`), quando veio."""

    def __init__(
        self,
        message: str,
        *,
        status_code: int | None = None,
        body: str | None = None,
        error: str | None = None,
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.body = body
        self.error = error
        self.details = details


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


def _query(empresa: str, cursor: str | None, limite: int) -> dict[str, Any]:
    params: dict[str, Any] = {"empresaFaturadora": empresa, "limite": limite}
    if cursor:
        params["cursor"] = cursor
    return params


def _corpo_de_erro(resp: httpx.Response) -> tuple[str | None, dict[str, Any] | None]:
    """`{error, details?}` do Flow; corpo que não é isso → (None, None)."""
    try:
        corpo = resp.json()
    except ValueError:
        return None, None
    if not isinstance(corpo, dict):
        return None, None
    erro = corpo.get("error") if isinstance(corpo.get("error"), str) else None
    detalhes = corpo.get("details") if isinstance(corpo.get("details"), dict) else None
    return erro, detalhes


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

    def list_cadastros_pendentes(self, limite: int = 20) -> PendentesResposta:
        """Fila de cadastros aprovados no Flow. Só leitura: nada é confirmado."""
        try:
            resp = self._client.get(_CADASTROS_PENDENTES_PATH, params={"limite": limite})
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

    # ─── contrato precos v1 ────────────────────────────────────────────────

    def get_precos_tabela(
        self, empresa: str, cursor: str | None = None, limite: int = 500
    ) -> TabelaPaginaV1:
        """Rota (a): uma página da tabela, filtrada pela empresa faturadora."""
        return self._precos(
            "GET", _PRECOS_TABELA_PATH, TabelaPaginaV1, params=_query(empresa, cursor, limite)
        )

    def post_precos_aprovacao(self, corpo: dict[str, Any]) -> AprovacaoRespostaV1:
        """Rota (b): pedido aguardando preço ou retirada. Idempotente no Flow por
        (empresa, externalId, revisao); a chave do cabeçalho segue a mesma tupla."""
        chave = (
            f"precos-{corpo.get('tipo')}-{corpo.get('empresaFaturadoraCnpj')}-"
            f"{corpo.get('externalId')}-{corpo.get('revisao')}"
        )
        return self._precos(
            "POST", _PRECOS_APROVACOES_PATH, AprovacaoRespostaV1, corpo=corpo, idem=chave
        )

    def get_precos_decisoes(
        self, empresa: str, cursor: str | None = None, limite: int = 100
    ) -> DecisoesPaginaV1:
        """Rota (c): decisões ainda não confirmadas daquela empresa."""
        return self._precos(
            "GET",
            _PRECOS_DECISOES_PATH,
            DecisoesPaginaV1,
            params=_query(empresa, cursor, limite),
        )

    def post_precos_confirmar(self, corpo: dict[str, Any]) -> ConfirmacaoRespostaV1:
        """Rota (c): confirma o lote de decisões (resposta sempre 200 por item)."""
        digest = hashlib.sha256(json.dumps(corpo, sort_keys=True).encode()).hexdigest()[:16]
        return self._precos(
            "POST",
            _PRECOS_CONFIRMAR_PATH,
            ConfirmacaoRespostaV1,
            corpo=corpo,
            idem=f"precos-confirmar-{digest}",
        )

    def _precos(self, metodo, path, modelo, *, params=None, corpo=None, idem=None):
        """GET com `read_only_policy` (429 e 5xx); POST com `idempotent_post_policy`.
        Falha → `FlowPCPClientError` com `status_code`, `error` e `details`
        (rede: `status_code` None; resposta fora do schema: `resposta_fora_do_contrato`)."""
        try:
            if metodo == "GET":
                resp = self._client.get(path, params=params, retry_policy=read_only_policy())
            else:
                resp = self._client.post_json(
                    path, json=corpo, idempotency_key=idem, retry_policy=idempotent_post_policy()
                )
        except (HttpError, httpx.HTTPError) as exc:
            raise FlowPCPClientError(
                f"{metodo} {path} falhou: {exc!r}",
                status_code=getattr(exc, "status_code", None),
                body=getattr(exc, "body", None),
            ) from exc
        if not resp.is_success:
            erro, detalhes = _corpo_de_erro(resp)
            raise FlowPCPClientError(
                f"{metodo} {path} status {resp.status_code} ({erro})",
                status_code=resp.status_code,
                body=(resp.text or "")[:500],
                error=erro,
                details=detalhes,
            )
        try:
            return modelo.model_validate(resp.json())
        except ValueError as exc:
            raise FlowPCPClientError(
                f"{metodo} {path}: resposta fora do contrato: {str(exc)[:300]}",
                status_code=resp.status_code,
                body=(resp.text or "")[:500],
                error="resposta_fora_do_contrato",
            ) from exc

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
