from __future__ import annotations

import json

import httpx
import pytest

from app.http.client import OutboundClient
from app.integrations.flowpcp.client import FlowPCPClient, FlowPCPClientError
from app.integrations.flowpcp.schema import (
    AcaoReconciliacao,
    ConfirmarReconciliacaoRequest,
)

TENANT = "1798c3c5-0fb6-4edb-a523-e13fb5bf52a0"
TOKEN = "test-service-token"


def _client(handler) -> FlowPCPClient:
    outbound = OutboundClient(
        base_url="https://flow.test",
        default_headers={
            "X-Service-Token": TOKEN,
            "X-Tenant-Id": TENANT,
            "Content-Type": "application/json",
        },
        transport=httpx.MockTransport(handler),
    )
    return FlowPCPClient(
        base_url="https://flow.test",
        service_token=TOKEN,
        tenant_id=TENANT,
        outbound=outbound,
    )


def test_list_decisoes_sends_auth_and_parses():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["method"] = request.method
        seen["path"] = request.url.path
        seen["token"] = request.headers.get("X-Service-Token")
        seen["tenant"] = request.headers.get("X-Tenant-Id")
        seen["cursor"] = request.url.params.get("cursor")
        return httpx.Response(200, json={"decisoes": [], "proximo_cursor": None})

    resp = _client(handler).list_decisoes(cursor="2026-06-22T14:30:00.000Z")
    assert seen["method"] == "GET"
    assert seen["path"] == "/api/portal-pedidos/decisoes"
    assert seen["token"] == TOKEN
    assert seen["tenant"] == TENANT
    assert seen["cursor"] == "2026-06-22T14:30:00.000Z"
    assert resp.decisoes == []


def test_confirmar_posts_to_id_path_and_handles_409():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "POST"
        assert request.url.path == "/api/portal-pedidos/decisoes/dec-1/confirmar-reconciliacao"
        body = json.loads(request.content)
        assert body["acao"] == "data_atualizada"
        return httpx.Response(409, json={"error": "ja_reconciliado"})

    out = _client(handler).confirmar_reconciliacao(
        "dec-1",
        ConfirmarReconciliacaoRequest(acao=AcaoReconciliacao.DATA_ATUALIZADA),
    )
    assert out["conflict"] is True


def test_send_order_raises_on_500():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, json={"error": "boom"})

    from app.integrations.flowpcp.schema import (
        ClienteRecebimento,
        ItemRecebimento,
        OrigemRecebimento,
        RecebimentoRequest,
    )

    req = RecebimentoRequest(
        externalId="imp-1",
        fornecedor="Centauro",
        pedidoNumero="AW097",
        emitidoEm="2026-06-15T10:00:00.000Z",
        cliente=ClienteRecebimento(nome="MM", cnpj="12345678000190"),
        itens=[ItemRecebimento(descricao="meia", quantidade=10)],
        origem=OrigemRecebimento(
            importadorVersao="1.0.0",
            arquivoOriginal="p.pdf",
            parserUsado="Test",
            confiancaParser="alta",
        ),
    )
    with pytest.raises(FlowPCPClientError):
        _client(handler).send_order(req, idempotency_key="imp-1")
