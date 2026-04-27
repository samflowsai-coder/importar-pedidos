"""Retry policies for outbound HTTP. Conservative by design.

Three flavors. Pick the one that matches the call's idempotency:

- `read_only_policy`         — GETs and other side-effect-free calls.
                               Up to 3 attempts, exponential backoff.
- `idempotent_post_policy`   — POSTs that the caller is sure are safe to
                               replay (idempotency-key in body, or the
                               server explicitly handles dedup). 5xx + conn
                               errors only; never 4xx.
- `llm_call_policy`          — pessimistic. 1 retry only, on connection
                               errors / 502 / 503 / 504. The model may have
                               billed the request — never retry on 4xx, and
                               never retry more than once.

Tenacity composes the policy as a `Retrying` instance the client invokes
imperatively, so each attempt can log structured context (status, duration).
"""
from __future__ import annotations

from dataclasses import dataclass, field

import httpx
from tenacity import (
    Retrying,
    retry_if_exception,
    stop_after_attempt,
    wait_exponential_jitter,
)

# Retryable network-level errors (transient): connection refused, timeouts,
# DNS hiccups, mid-request stream errors. Never includes auth/permission.
_RETRYABLE_EXC = (
    httpx.ConnectError,
    httpx.ConnectTimeout,
    httpx.ReadTimeout,
    httpx.WriteTimeout,
    httpx.PoolTimeout,
    httpx.RemoteProtocolError,
)


@dataclass(frozen=True)
class RetryPolicy:
    """Declarative shape; client builds a `Retrying` from this on demand."""

    max_attempts: int
    retry_on_status: frozenset[int] = field(default_factory=frozenset)
    wait_initial_seconds: float = 0.5
    wait_max_seconds: float = 8.0
    wait_jitter_seconds: float = 0.5
    retry_on_exceptions: tuple[type[BaseException], ...] = _RETRYABLE_EXC

    def should_retry_status(self, status_code: int) -> bool:
        return status_code in self.retry_on_status

    def build(self) -> Retrying:
        """Build a tenacity Retrying instance for this policy.

        The client will raise an internal `_RetryableHttpStatusError` to surface
        retryable status codes through tenacity's exception path.
        """
        return Retrying(
            stop=stop_after_attempt(self.max_attempts),
            wait=wait_exponential_jitter(
                initial=self.wait_initial_seconds,
                max=self.wait_max_seconds,
                jitter=self.wait_jitter_seconds,
            ),
            retry=retry_if_exception(_should_retry_exception(self)),
            reraise=True,
        )


def _should_retry_exception(policy: RetryPolicy):
    def predicate(exc: BaseException) -> bool:
        if isinstance(exc, policy.retry_on_exceptions):
            return True
        # Local marker for retryable-status-code paths
        from app.http.client import _RetryableHttpStatusError  # avoid circular import
        if isinstance(exc, _RetryableHttpStatusError):
            return True
        return False
    return predicate


# 502/503/504 are the canonical transient gateway errors. 500 is murky —
# could be a real bug; we still retry once on idempotent calls but never on
# LLM (cost concern). 429 is "back off and retry" — appropriate for read-only
# but risky on POSTs without explicit idempotency support.
_TRANSIENT_5XX: frozenset[int] = frozenset({502, 503, 504})
_TRANSIENT_5XX_AND_500: frozenset[int] = frozenset({500, 502, 503, 504})
_TRANSIENT_5XX_AND_429: frozenset[int] = frozenset({429, 500, 502, 503, 504})


def read_only_policy() -> RetryPolicy:
    """GET / HEAD / safe reads. Tolerates 429 (rate-limit) and 5xx."""
    return RetryPolicy(
        max_attempts=3,
        retry_on_status=_TRANSIENT_5XX_AND_429,
        wait_initial_seconds=0.5,
        wait_max_seconds=8.0,
    )


def idempotent_post_policy() -> RetryPolicy:
    """POSTs the caller has marked safe to replay (idempotency-key)."""
    return RetryPolicy(
        max_attempts=3,
        retry_on_status=_TRANSIENT_5XX_AND_500,
        wait_initial_seconds=1.0,
        wait_max_seconds=15.0,
    )


def llm_call_policy() -> RetryPolicy:
    """LLM completions. Pessimistic — 1 retry only, gateway errors only.

    Rationale: the model may have already billed the request. Retry too
    eagerly and you double-charge with no guarantee of better output.
    Never on 4xx (your prompt is malformed; retry won't help).
    """
    return RetryPolicy(
        max_attempts=2,
        retry_on_status=_TRANSIENT_5XX,
        wait_initial_seconds=1.0,
        wait_max_seconds=4.0,
    )


__all__ = [
    "RetryPolicy",
    "idempotent_post_policy",
    "llm_call_policy",
    "read_only_policy",
]
