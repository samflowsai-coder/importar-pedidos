from __future__ import annotations

import json

import httpx
import pytest

from app.http.client import OutboundClient
from app.http.policies import idempotent_post_policy
from app.integrations.flowpcp.client import (
    SYNC_PATH,
    FlowPCPClient,
    FlowPCPClientError,
)


def _make_client(handler) -> FlowPCPClient:
    transport = httpx.MockTransport(handler)
    outbound = OutboundClient(
        base_url="https://flowpcp.test",
        retry_policy=idempotent_post_policy(),
        default_headers={"Content-Type": "application/json"},
        transport=transport,
    )
    return FlowPCPClient(
        base_url="https://flowpcp.test",
        api_key="pp_live_TEST",
        tenant_id="00000000-0000-0000-0000-000000000001",
        outbound=outbound,
    )


def test_sync_happy_path():
    captured = {}

    def handler(req: httpx.Request) -> httpx.Response:
        captured["headers"] = dict(req.headers)
        captured["body"] = json.loads(req.content)
        captured["url"] = str(req.url)
        return httpx.Response(200, json={
            "sync_id": "01HX",
            "applied": {"produtos": 1, "componentes": 0, "tombstones": 0},
            "skipped": 0,
            "errors": [],
        })

    c = _make_client(handler)
    resp = c.sync_products(
        produtos=[{"codigo": "1", "nome": "X", "unidade": "un", "ativo": True}],
        componentes=[],
        sync_id="01HX",
        trace_id="t-1",
    )
    assert resp.applied == {"produtos": 1, "componentes": 0, "tombstones": 0}
    assert SYNC_PATH in captured["url"]
    assert captured["headers"]["authorization"] == "Bearer pp_live_TEST"
    assert captured["headers"]["idempotency-key"] == "01HX"
    assert captured["headers"]["x-trace-id"] == "t-1"
    assert captured["body"]["tenant_id"] == "00000000-0000-0000-0000-000000000001"


def test_sync_401_raises_with_status():
    def handler(req): return httpx.Response(401, json={"error": "invalid_api_key"})
    c = _make_client(handler)
    with pytest.raises(FlowPCPClientError) as exc:
        c.sync_products(produtos=[], componentes=[], sync_id="x", trace_id=None)
    assert exc.value.status_code == 401


def test_sync_403_tenant_mismatch():
    def handler(req): return httpx.Response(403, json={"error": "tenant_mismatch"})
    c = _make_client(handler)
    with pytest.raises(FlowPCPClientError) as exc:
        c.sync_products(produtos=[], componentes=[], sync_id="x", trace_id=None)
    assert exc.value.status_code == 403


def test_sync_5xx_after_retries_raises():
    def handler(req): return httpx.Response(503)
    c = _make_client(handler)
    with pytest.raises(FlowPCPClientError):
        c.sync_products(produtos=[], componentes=[], sync_id="x", trace_id=None)


def test_sync_returns_partial_with_errors():
    def handler(req): return httpx.Response(200, json={
        "sync_id": "x",
        "applied": {"produtos": 1, "componentes": 0, "tombstones": 0},
        "skipped": 1,
        "errors": [{"codigo": "42", "reason": "componente_filho_inexistente"}],
    })
    c = _make_client(handler)
    resp = c.sync_products(produtos=[], componentes=[], sync_id="x", trace_id=None)
    assert resp.skipped == 1
    assert resp.errors[0].codigo == "42"


def test_health_endpoint():
    def handler(req: httpx.Request) -> httpx.Response:
        assert "/api/portal-pedidos/health" in str(req.url)
        return httpx.Response(200, json={"ok": True, "tenant_id": "..."})
    c = _make_client(handler)
    assert c.health() is True
