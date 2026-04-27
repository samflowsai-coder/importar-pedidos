"""Per-order trace_id, propagated via contextvars.

A trace_id is a UUID4 minted at the boundary where a pedido enters the system
(commit, send-to-fire, webhook, worker tick). It travels with every log line,
every outbound HTTP call (X-Trace-Id), and is stamped on every lifecycle event.
Without this, debugging across Portal + Firebird + Gestor + Apontaê is hopeless.
"""
from __future__ import annotations

import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar, Token

_trace_id_var: ContextVar[str | None] = ContextVar("trace_id", default=None)


def new_trace_id() -> str:
    return uuid.uuid4().hex


def current_trace_id() -> str | None:
    return _trace_id_var.get()


def set_trace_id(value: str | None) -> Token:
    """Imperatively set trace_id. Prefer `with_trace_id()` context manager."""
    return _trace_id_var.set(value)


def reset_trace_id(token: Token) -> None:
    _trace_id_var.reset(token)


@contextmanager
def with_trace_id(trace_id: str | None = None) -> Iterator[str]:
    """Bind a trace_id to the current context. Generates one if not provided.

    Yields the active trace_id. Restores the previous value on exit.
    """
    tid = trace_id or new_trace_id()
    token = _trace_id_var.set(tid)
    try:
        yield tid
    finally:
        _trace_id_var.reset(token)


__all__ = [
    "current_trace_id",
    "new_trace_id",
    "reset_trace_id",
    "set_trace_id",
    "with_trace_id",
]
