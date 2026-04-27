"""Outbound HTTP layer.

Single client wrapper around httpx + tenacity. Public surface:

    from app.http import OutboundClient, RetryPolicy
    from app.http import idempotent_post_policy, llm_call_policy, read_only_policy

Every outbound call goes through this module. Three things it gives you:

1. **Retry with sane defaults**, varying by call type. LLM calls retry only on
   connection errors / 5xx — never on 4xx, since the model may have already
   billed the request.
2. **trace_id propagation** — `X-Trace-Id` header is injected automatically
   from the `app.observability.trace` ContextVar.
3. **Structured logging** — every attempt and final outcome is logged with
   trace_id, target host, status, duration.
"""
from app.http.client import HttpError, OutboundClient
from app.http.policies import (
    RetryPolicy,
    idempotent_post_policy,
    llm_call_policy,
    read_only_policy,
)

__all__ = [
    "HttpError",
    "OutboundClient",
    "RetryPolicy",
    "idempotent_post_policy",
    "llm_call_policy",
    "read_only_policy",
]
