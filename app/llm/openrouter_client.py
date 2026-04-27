"""OpenRouter chat-completions client built on OutboundClient.

OpenRouter speaks the OpenAI chat-completions wire format. We hit
`POST /api/v1/chat/completions` directly — no `openai` SDK required, which
removes a sizable dependency and gives us full control over retry/timeout,
trace_id propagation, and observability.

Public surface:

    client = OpenRouterClient(api_key=os.environ["OPENROUTER_API_KEY"])
    text = client.chat_completion(
        model="google/gemini-flash-1.5",
        messages=[{"role": "user", "content": "..."}],
        response_format={"type": "json_object"},
        max_tokens=2048,
        temperature=0,
    )

Returns the assistant's `content` string. Raises `LLMUnavailableError` on
configuration / connectivity / non-2xx after retries — callers (the LLM
fallback parser) translate that to "skip the file" behaviour.
"""
from __future__ import annotations

import os
from typing import Any

from app.http.client import HttpError, OutboundClient
from app.http.policies import llm_call_policy

OPENROUTER_BASE_URL = "https://openrouter.ai/api/v1"
DEFAULT_TIMEOUT = 60.0  # LLMs can take a while; budget generously
APP_TITLE = "importar-pedidos"


class LLMUnavailableError(Exception):
    """Raised when the LLM call cannot be completed (config, network, 4xx/5xx)."""


class OpenRouterClient:
    """Thin wrapper. One instance per process is fine — connection pooled."""

    def __init__(
        self,
        *,
        api_key: str | None = None,
        base_url: str = OPENROUTER_BASE_URL,
        timeout: float = DEFAULT_TIMEOUT,
        outbound: OutboundClient | None = None,
    ) -> None:
        # Lazy validation: caller may construct without a key for tests that
        # inject `outbound`. Real chat_completion() calls require a key.
        self._api_key = api_key
        if outbound is None:
            outbound = OutboundClient(
                base_url=base_url,
                timeout=timeout,
                retry_policy=llm_call_policy(),
                default_headers={
                    "Content-Type": "application/json",
                    "X-Title": APP_TITLE,
                },
            )
        self._client = outbound

    @classmethod
    def from_env(cls, **kwargs: Any) -> OpenRouterClient:
        """Build from `OPENROUTER_API_KEY` env var. Raises if unset."""
        key = os.environ.get("OPENROUTER_API_KEY")
        if not key:
            raise LLMUnavailableError("OPENROUTER_API_KEY not set")
        return cls(api_key=key, **kwargs)

    def close(self) -> None:
        self._client.close()

    def chat_completion(
        self,
        *,
        model: str,
        messages: list[dict[str, Any]],
        response_format: dict[str, Any] | None = None,
        max_tokens: int = 2048,
        temperature: float = 0.0,
    ) -> str:
        """Call POST /chat/completions and return the assistant content string."""
        if not self._api_key:
            raise LLMUnavailableError("OPENROUTER_API_KEY not set")

        body: dict[str, Any] = {
            "model": model,
            "messages": messages,
            "max_tokens": max_tokens,
            "temperature": temperature,
        }
        if response_format is not None:
            body["response_format"] = response_format

        try:
            response = self._client.post_json(
                "/chat/completions",
                json=body,
                headers={"Authorization": f"Bearer {self._api_key}"},
            )
        except HttpError as exc:
            raise LLMUnavailableError(f"OpenRouter HTTP error: {exc}") from exc
        except Exception as exc:  # noqa: BLE001 — connection/timeouts after retries
            raise LLMUnavailableError(
                f"OpenRouter unreachable: {type(exc).__name__}: {exc}"
            ) from exc

        if not response.is_success:
            preview = (response.text or "")[:500]
            raise LLMUnavailableError(
                f"OpenRouter status={response.status_code}: {preview}"
            )

        try:
            data = response.json()
        except ValueError as exc:
            raise LLMUnavailableError(
                f"OpenRouter returned non-JSON: {response.text[:500]}"
            ) from exc

        try:
            return data["choices"][0]["message"]["content"] or ""
        except (KeyError, IndexError, TypeError) as exc:
            raise LLMUnavailableError(
                f"OpenRouter response missing choices/message/content: {data}"
            ) from exc


__all__ = ["LLMUnavailableError", "OpenRouterClient"]
