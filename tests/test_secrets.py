"""Tests for app.security.secrets."""
from __future__ import annotations

from app.security.secrets import _set_backend, get_secret


def test_get_secret_from_env(monkeypatch):
    monkeypatch.setenv("MY_TEST_SECRET", "hello")
    assert get_secret("MY_TEST_SECRET") == "hello"


def test_get_secret_default_when_absent(monkeypatch):
    monkeypatch.delenv("ABSENT_SECRET_XYZ", raising=False)
    assert get_secret("ABSENT_SECRET_XYZ", default="fallback") == "fallback"


def test_get_secret_none_when_absent_and_no_default(monkeypatch):
    monkeypatch.delenv("ABSENT_SECRET_XYZ", raising=False)
    assert get_secret("ABSENT_SECRET_XYZ") is None


def test_custom_backend():
    class _StaticBackend:
        def get(self, name: str, default=None):
            return f"static:{name}"

    _set_backend(_StaticBackend())
    try:
        assert get_secret("FOO") == "static:FOO"
    finally:
        # Restore env backend.
        from app.security.secrets import _EnvBackend  # noqa: PLC0415
        _set_backend(_EnvBackend())


def test_backend_protocol_satisfied():
    """SecretsBackend is a runtime-checkable Protocol — env backend passes."""
    from app.security.secrets import SecretsBackend, _EnvBackend  # noqa: PLC0415

    backend = _EnvBackend()
    assert isinstance(backend, SecretsBackend)
