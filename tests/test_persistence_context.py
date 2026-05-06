"""ContextVar de ambiente ativo."""
from __future__ import annotations

import pytest

from app.persistence import context


def test_current_returns_none_by_default():
    assert context.current() is None


def test_active_env_sets_and_clears():
    with context.active_env("env-1", "mm"):
        assert context.current() == {"id": "env-1", "slug": "mm"}
        assert context.current_env_id() == "env-1"
        assert context.current_env_slug() == "mm"
    assert context.current() is None


def test_active_env_nested():
    with context.active_env("env-1", "mm"):
        with context.active_env("env-2", "nasmar"):
            assert context.current_env_slug() == "nasmar"
        # bloco interno saiu — volta ao MM
        assert context.current_env_slug() == "mm"


def test_current_or_raise_when_unset():
    context.clear_active_env()
    with pytest.raises(context.NoActiveEnvironmentError):
        context.current_or_raise()
    with pytest.raises(context.NoActiveEnvironmentError):
        context.current_env_id()
    with pytest.raises(context.NoActiveEnvironmentError):
        context.current_env_slug()


def test_isolation_between_threads():
    """Mudança em uma thread não afeta outra."""
    import threading

    seen: dict[str, str | None] = {}

    def worker(name: str):
        with context.active_env(f"id-{name}", name):
            import time
            time.sleep(0.05)
            seen[name] = context.current_env_slug()

    t1 = threading.Thread(target=worker, args=("mm",))
    t2 = threading.Thread(target=worker, args=("nasmar",))
    t1.start(); t2.start()
    t1.join(); t2.join()

    assert seen == {"mm": "mm", "nasmar": "nasmar"}
