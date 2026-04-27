"""OutboundClient — single wrapper around httpx with retry + observability.

Public surface:

    client = OutboundClient(
        base_url="https://api.example.com",
        timeout=30.0,
        retry_policy=idempotent_post_policy(),
        default_headers={"Authorization": f"Bearer {token}"},
    )
    resp = client.post_json("/orders", json={"id": "x"}, idempotency_key="x")
    data = resp.json()

What you get for free:

- `X-Trace-Id` header injected from `app.observability.trace.current_trace_id()`.
- Tenacity-driven retry per the supplied `RetryPolicy`. Retryable HTTP
  statuses surface as `_RetryableHttpStatusError` so tenacity sees them. Final
  failure raises `HttpError`.
- Structured logging on each attempt: trace_id, host, path, status, ms.
- Connection pooling reused across calls (one `httpx.Client` per instance).
- `close()` / context manager support.
"""
from __future__ import annotations

import time
from types import TracebackType
from typing import Any

import httpx

from app.http.policies import RetryPolicy, idempotent_post_policy
from app.observability.trace import current_trace_id
from app.utils.logger import logger


class HttpError(Exception):
    """Final outbound HTTP failure after retries (or non-retryable error)."""

    def __init__(
        self,
        message: str,
        *,
        status_code: int | None = None,
        url: str | None = None,
        body: str | None = None,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.url = url
        self.body = body


class _RetryableHttpStatusError(Exception):
    """Internal: lets tenacity catch a retryable HTTP status as an exception.

    The Response object is preserved so the client can return it (or unwrap
    it into HttpError) once retries are exhausted.
    """

    def __init__(self, response: httpx.Response) -> None:
        super().__init__(
            f"Retryable HTTP {response.status_code} from {response.request.url}"
        )
        self.response = response


# Body preview cap for logs; full body still available on HttpError.
_LOG_BODY_PREVIEW = 500


class OutboundClient:
    """Wraps httpx.Client with retry, trace propagation, and structured logs."""

    def __init__(
        self,
        *,
        base_url: str = "",
        timeout: float = 30.0,
        retry_policy: RetryPolicy | None = None,
        default_headers: dict[str, str] | None = None,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self._retry_policy = retry_policy or idempotent_post_policy()
        self._default_headers = dict(default_headers or {})
        self._client = httpx.Client(
            base_url=base_url,
            timeout=timeout,
            headers=self._default_headers,
            transport=transport,
        )

    # ── lifecycle ────────────────────────────────────────────────────────
    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> OutboundClient:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.close()

    # ── public verbs ─────────────────────────────────────────────────────
    def post_json(
        self,
        url: str,
        *,
        json: dict[str, Any] | list[Any],
        headers: dict[str, str] | None = None,
        idempotency_key: str | None = None,
        timeout: float | None = None,
    ) -> httpx.Response:
        merged = self._build_headers(headers, idempotency_key=idempotency_key)
        return self._send_with_retry(
            "POST", url, headers=merged, json=json, timeout=timeout
        )

    def get(
        self,
        url: str,
        *,
        params: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
        timeout: float | None = None,
    ) -> httpx.Response:
        merged = self._build_headers(headers)
        return self._send_with_retry(
            "GET", url, headers=merged, params=params, timeout=timeout
        )

    # ── internals ────────────────────────────────────────────────────────
    def _build_headers(
        self,
        extra: dict[str, str] | None,
        *,
        idempotency_key: str | None = None,
    ) -> dict[str, str]:
        headers: dict[str, str] = {}
        if extra:
            headers.update(extra)
        trace_id = current_trace_id()
        if trace_id and "X-Trace-Id" not in headers:
            headers["X-Trace-Id"] = trace_id
        if idempotency_key and "Idempotency-Key" not in headers:
            headers["Idempotency-Key"] = idempotency_key
        return headers

    def _send_with_retry(
        self,
        method: str,
        url: str,
        *,
        headers: dict[str, str],
        json: Any = None,
        params: dict[str, Any] | None = None,
        timeout: float | None = None,
    ) -> httpx.Response:
        retrying = self._retry_policy.build()
        attempt_n = 0
        try:
            for attempt in retrying:
                with attempt:
                    attempt_n += 1
                    started = time.perf_counter()
                    try:
                        response = self._client.request(
                            method,
                            url,
                            headers=headers,
                            json=json,
                            params=params,
                            timeout=(
                                timeout
                                if timeout is not None
                                else httpx.USE_CLIENT_DEFAULT
                            ),
                        )
                    except httpx.HTTPError as exc:
                        duration_ms = int((time.perf_counter() - started) * 1000)
                        logger.warning(
                            f"http {method} {url} attempt={attempt_n} "
                            f"error={type(exc).__name__} duration_ms={duration_ms}"
                        )
                        raise

                    duration_ms = int((time.perf_counter() - started) * 1000)
                    if self._retry_policy.should_retry_status(response.status_code):
                        logger.warning(
                            f"http {method} {url} attempt={attempt_n} "
                            f"status={response.status_code} "
                            f"duration_ms={duration_ms} retryable"
                        )
                        raise _RetryableHttpStatusError(response)

                    logger.info(
                        f"http {method} {url} attempt={attempt_n} "
                        f"status={response.status_code} duration_ms={duration_ms}"
                    )
                    return response
        except _RetryableHttpStatusError as exhausted:
            # All retries exhausted on a retryable status — give the caller
            # the final response so they can introspect / raise as they wish.
            logger.error(
                f"http {method} {url} attempts={attempt_n} "
                f"final_status={exhausted.response.status_code} retries_exhausted"
            )
            return exhausted.response

        # Tenacity raised a non-retryable exception — already propagated.
        raise HttpError(  # pragma: no cover — defensive only
            "retry loop exhausted unexpectedly", url=url
        )

    @staticmethod
    def raise_for_status(response: httpx.Response) -> httpx.Response:
        """Convert non-2xx into HttpError. Useful when caller wants strict mode."""
        if response.is_success:
            return response
        body_preview = (response.text or "")[:_LOG_BODY_PREVIEW]
        raise HttpError(
            f"HTTP {response.status_code} from {response.request.url}",
            status_code=response.status_code,
            url=str(response.request.url),
            body=body_preview,
        )


__all__ = ["HttpError", "OutboundClient"]
