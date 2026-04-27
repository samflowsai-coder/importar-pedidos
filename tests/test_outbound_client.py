"""Tests for app.http.client (OutboundClient) and policy behavior.

Uses httpx.MockTransport to assert exact retry / header / status behavior
without hitting the network.
"""
from __future__ import annotations

import json

import httpx
import pytest

from app.http import OutboundClient
from app.http.policies import (
    RetryPolicy,
    idempotent_post_policy,
    llm_call_policy,
    read_only_policy,
)
from app.observability.trace import with_trace_id


def _transport(handler):
    return httpx.MockTransport(handler)


# ── trace_id header injection ────────────────────────────────────────────


def test_post_injects_trace_id_from_contextvar():
    seen: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(dict(request.headers))
        return httpx.Response(200, json={"ok": True})

    with OutboundClient(
        base_url="http://test", retry_policy=read_only_policy(),
        transport=_transport(handler),
    ) as client:
        with with_trace_id("trace-1234"):
            client.post_json("/x", json={"a": 1})

    assert seen[0]["x-trace-id"] == "trace-1234"


def test_post_omits_trace_id_when_no_context():
    seen: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(dict(request.headers))
        return httpx.Response(200, json={})

    with OutboundClient(
        base_url="http://test", retry_policy=read_only_policy(),
        transport=_transport(handler),
    ) as client:
        client.post_json("/x", json={})

    assert "x-trace-id" not in seen[0]


def test_idempotency_key_header_set():
    seen: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(dict(request.headers))
        return httpx.Response(200, json={})

    with OutboundClient(
        base_url="http://test",
        retry_policy=idempotent_post_policy(),
        transport=_transport(handler),
    ) as client:
        client.post_json("/x", json={}, idempotency_key="key-abc")

    assert seen[0]["idempotency-key"] == "key-abc"


def test_default_headers_propagated():
    seen: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(dict(request.headers))
        return httpx.Response(200, json={})

    with OutboundClient(
        base_url="http://test",
        default_headers={"Authorization": "Bearer token-xyz"},
        retry_policy=read_only_policy(),
        transport=_transport(handler),
    ) as client:
        client.get("/x")

    assert seen[0]["authorization"] == "Bearer token-xyz"


# ── retry on transient status codes ──────────────────────────────────────


def test_retries_on_503_and_eventually_succeeds():
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] < 3:
            return httpx.Response(503, text="busy")
        return httpx.Response(200, json={"ok": True})

    fast = RetryPolicy(
        max_attempts=4, retry_on_status=frozenset({503}),
        wait_initial_seconds=0.0, wait_max_seconds=0.0, wait_jitter_seconds=0.0,
    )
    with OutboundClient(
        base_url="http://test", retry_policy=fast,
        transport=_transport(handler),
    ) as client:
        resp = client.post_json("/x", json={})

    assert resp.status_code == 200
    assert calls["n"] == 3


def test_returns_final_response_when_retries_exhausted():
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(503, text="still busy")

    fast = RetryPolicy(
        max_attempts=2, retry_on_status=frozenset({503}),
        wait_initial_seconds=0.0, wait_max_seconds=0.0, wait_jitter_seconds=0.0,
    )
    with OutboundClient(
        base_url="http://test", retry_policy=fast,
        transport=_transport(handler),
    ) as client:
        resp = client.post_json("/x", json={})

    # Caller gets back the final response (still 503), can decide what to do
    assert resp.status_code == 503
    assert calls["n"] == 2


def test_does_not_retry_on_4xx():
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(400, text="bad request")

    with OutboundClient(
        base_url="http://test", retry_policy=idempotent_post_policy(),
        transport=_transport(handler),
    ) as client:
        resp = client.post_json("/x", json={})

    assert resp.status_code == 400
    assert calls["n"] == 1, "4xx must not trigger retry"


def test_llm_policy_does_not_retry_on_500():
    """500 is in idempotent_post but NOT in llm_call (cost concern)."""
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(500, text="server error")

    with OutboundClient(
        base_url="http://test", retry_policy=llm_call_policy(),
        transport=_transport(handler),
    ) as client:
        resp = client.post_json("/x", json={})

    assert resp.status_code == 500
    assert calls["n"] == 1


def test_llm_policy_retries_on_503_once():
    """LLM policy = max_attempts=2, so one retry."""
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(503, text="busy")

    fast = RetryPolicy(
        max_attempts=2, retry_on_status=frozenset({503}),
        wait_initial_seconds=0.0, wait_max_seconds=0.0, wait_jitter_seconds=0.0,
    )
    with OutboundClient(
        base_url="http://test", retry_policy=fast,
        transport=_transport(handler),
    ) as client:
        client.post_json("/x", json={})

    assert calls["n"] == 2


def test_retries_on_connection_error_then_succeeds():
    """Network exception during the request triggers retry."""
    state = {"calls": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        state["calls"] += 1
        if state["calls"] == 1:
            raise httpx.ConnectError("simulated network blip")
        return httpx.Response(200, json={"ok": True})

    fast = RetryPolicy(
        max_attempts=3, retry_on_status=frozenset(),
        wait_initial_seconds=0.0, wait_max_seconds=0.0, wait_jitter_seconds=0.0,
    )
    with OutboundClient(
        base_url="http://test", retry_policy=fast,
        transport=_transport(handler),
    ) as client:
        resp = client.post_json("/x", json={})

    assert resp.status_code == 200
    assert state["calls"] == 2


def test_raise_for_status_helper():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, text="not found")

    with OutboundClient(
        base_url="http://test", retry_policy=read_only_policy(),
        transport=_transport(handler),
    ) as client:
        resp = client.get("/x")
        from app.http import HttpError
        with pytest.raises(HttpError) as ei:
            OutboundClient.raise_for_status(resp)
        assert ei.value.status_code == 404


# ── OpenRouterClient via OutboundClient ──────────────────────────────────


def test_openrouter_client_sends_bearer_and_returns_content():
    from app.llm.openrouter_client import OpenRouterClient

    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["headers"] = dict(request.headers)
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, json={
            "choices": [{"message": {"content": "hello world"}}],
        })

    outbound = OutboundClient(
        base_url="http://test",
        retry_policy=llm_call_policy(),
        default_headers={"X-Title": "importar-pedidos"},
        transport=_transport(handler),
    )
    client = OpenRouterClient(api_key="test-key", outbound=outbound)
    text = client.chat_completion(
        model="google/gemini-flash-1.5",
        messages=[{"role": "user", "content": "hi"}],
        response_format={"type": "json_object"},
    )

    assert text == "hello world"
    assert seen["headers"]["authorization"] == "Bearer test-key"
    assert seen["headers"]["x-title"] == "importar-pedidos"
    assert seen["body"]["model"] == "google/gemini-flash-1.5"
    assert seen["body"]["temperature"] == 0
    assert seen["body"]["response_format"] == {"type": "json_object"}


def test_openrouter_raises_on_non_2xx():
    from app.llm.openrouter_client import LLMUnavailableError, OpenRouterClient

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, text='{"error":"invalid api key"}')

    outbound = OutboundClient(
        base_url="http://test", retry_policy=llm_call_policy(),
        transport=_transport(handler),
    )
    client = OpenRouterClient(api_key="bad-key", outbound=outbound)
    with pytest.raises(LLMUnavailableError) as ei:
        client.chat_completion(
            model="m", messages=[{"role": "user", "content": "x"}],
        )
    assert "401" in str(ei.value)


def test_openrouter_raises_when_api_key_missing():
    from app.llm.openrouter_client import LLMUnavailableError, OpenRouterClient

    outbound = OutboundClient(
        base_url="http://test", retry_policy=llm_call_policy(),
        transport=_transport(lambda r: httpx.Response(200, json={})),
    )
    client = OpenRouterClient(api_key=None, outbound=outbound)
    with pytest.raises(LLMUnavailableError, match="API_KEY"):
        client.chat_completion(model="m", messages=[])


def test_openrouter_propagates_trace_id():
    from app.llm.openrouter_client import OpenRouterClient

    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["headers"] = dict(request.headers)
        return httpx.Response(200, json={
            "choices": [{"message": {"content": "ok"}}],
        })

    outbound = OutboundClient(
        base_url="http://test", retry_policy=llm_call_policy(),
        transport=_transport(handler),
    )
    client = OpenRouterClient(api_key="k", outbound=outbound)
    with with_trace_id("trace-llm-9"):
        client.chat_completion(model="m", messages=[{"role": "user", "content": "x"}])

    assert seen["headers"]["x-trace-id"] == "trace-llm-9"
