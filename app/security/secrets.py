"""Secrets abstraction layer.

Reads from env vars today; swap _BACKEND for VaultBackend without touching callers.
"""
from __future__ import annotations

import os
from typing import Protocol, runtime_checkable


@runtime_checkable
class SecretsBackend(Protocol):
    def get(self, name: str, default: str | None = None) -> str | None: ...


class _EnvBackend:
    def get(self, name: str, default: str | None = None) -> str | None:
        return os.environ.get(name, default)


_BACKEND: SecretsBackend = _EnvBackend()


def get_secret(name: str, default: str | None = None) -> str | None:
    """Return the secret value for *name*, or *default* if not set."""
    return _BACKEND.get(name, default)


def _set_backend(backend: SecretsBackend) -> None:
    """Override the backend. Only for tests."""
    global _BACKEND
    _BACKEND = backend
