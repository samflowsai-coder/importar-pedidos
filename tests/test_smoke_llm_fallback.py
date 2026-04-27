"""Smoke tests for app.llm.fallback_parser.

We do NOT call OpenRouter. The parser delegates to OpenRouterClient (the
new httpx-based wrapper); we mock its `chat_completion(...)` method and
verify:
- Empty extracted text → returns None (no API call).
- Markdown-wrapped JSON is parsed correctly.
- Unknown fields in the JSON are ignored (forward-compat with model drift).
- Provider exception → returns None (parser never raises).
"""
from __future__ import annotations

import json
from unittest.mock import MagicMock

from app.llm.fallback_parser import LLMFallbackParser, _extract_json
from app.llm.openrouter_client import LLMUnavailableError
from app.models.order import Order


def test_returns_none_when_no_text() -> None:
    parser = LLMFallbackParser()
    assert parser.parse({"text": ""}) is None
    assert parser.parse({"text": "   \n  "}) is None


def test_extract_json_handles_markdown_fences() -> None:
    raw = "```json\n{\"header\": {}, \"items\": []}\n```"
    assert _extract_json(raw) == {"header": {}, "items": []}


def test_extract_json_handles_plain_json() -> None:
    raw = '{"header": {"order_number": "X"}, "items": []}'
    assert _extract_json(raw) == {"header": {"order_number": "X"}, "items": []}


def test_parse_returns_order_on_success() -> None:
    payload = {
        "header": {"order_number": "PED-1", "customer_name": "ACME"},
        "items": [{"description": "Tenis", "quantity": 2, "unit_price": 50.0}],
    }
    parser = LLMFallbackParser()
    fake_client = MagicMock()
    fake_client.chat_completion.return_value = json.dumps(payload)
    parser._client = fake_client  # bypass network

    result = parser.parse({"text": "qualquer pedido"}, source_file="x.pdf")

    assert isinstance(result, Order)
    assert result.header.order_number == "PED-1"
    assert result.items[0].description == "Tenis"
    assert result.items[0].quantity == 2
    assert result.source_file == "x.pdf"
    # Verify the parser passed our defaults through to the client
    call = fake_client.chat_completion.call_args
    assert call.kwargs["temperature"] == 0
    assert call.kwargs["max_tokens"] == 2048
    assert call.kwargs["response_format"] == {"type": "json_object"}


def test_parse_ignores_unknown_fields_from_model() -> None:
    payload = {
        "header": {"order_number": "P1", "made_up_field": "x"},
        "items": [{"description": "I", "quantity": 1, "ghost_field": 42}],
    }
    parser = LLMFallbackParser()
    parser._client = MagicMock()
    parser._client.chat_completion.return_value = json.dumps(payload)

    result = parser.parse({"text": "..."})
    assert result is not None
    assert result.header.order_number == "P1"
    assert result.items[0].description == "I"


def test_parse_returns_none_on_provider_exception() -> None:
    parser = LLMFallbackParser()
    parser._client = MagicMock()
    parser._client.chat_completion.side_effect = LLMUnavailableError("rate limit")

    assert parser.parse({"text": "qualquer"}) is None  # never raises


def test_parse_returns_none_on_unexpected_exception() -> None:
    """Generic exceptions (e.g. malformed JSON from model) are still swallowed."""
    parser = LLMFallbackParser()
    parser._client = MagicMock()
    parser._client.chat_completion.return_value = "not actually json {{{ invalid"

    assert parser.parse({"text": "qualquer"}) is None


def test_model_env_override(monkeypatch) -> None:
    monkeypatch.setenv("OPENROUTER_MODEL", "anthropic/claude-haiku-3-5")
    assert LLMFallbackParser().model == "anthropic/claude-haiku-3-5"
    monkeypatch.delenv("OPENROUTER_MODEL", raising=False)
    assert LLMFallbackParser().model == "google/gemini-flash-1.5"
