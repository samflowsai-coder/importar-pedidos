"""Tests for app.web.middleware.rate_limit (token bucket)."""
from __future__ import annotations

import time
from pathlib import Path
from unittest.mock import patch

import pytest

from app.persistence import db
from app.web.middleware.rate_limit import check_and_consume


@pytest.fixture
def sqlite_tmp(tmp_path: Path):
    db.set_db_path(tmp_path / "app_state.db")
    db.reset_init_cache()
    db.init()
    yield
    db.set_db_path(None)
    db.reset_init_cache()


def test_first_request_allowed(sqlite_tmp):
    assert check_and_consume("test:192.0.0.1", capacity=5, refill_rate=1.0) is True


def test_blocks_after_capacity_exhausted(sqlite_tmp):
    key = "test:10.0.0.1"
    # Exhaust the bucket (capacity=3).
    for _ in range(3):
        assert check_and_consume(key, capacity=3, refill_rate=0.0) is True
    # One more — bucket is dry.
    assert check_and_consume(key, capacity=3, refill_rate=0.0) is False


def test_different_keys_are_isolated(sqlite_tmp):
    # Exhaust key A, key B should still be allowed.
    for _ in range(3):
        check_and_consume("test:A", capacity=3, refill_rate=0.0)
    assert check_and_consume("test:A", capacity=3, refill_rate=0.0) is False
    assert check_and_consume("test:B", capacity=3, refill_rate=0.0) is True


def test_tokens_refill_over_time(sqlite_tmp):
    key = "test:refill"
    # Use up all 2 tokens.
    assert check_and_consume(key, capacity=2, refill_rate=1.0) is True
    assert check_and_consume(key, capacity=2, refill_rate=1.0) is True
    assert check_and_consume(key, capacity=2, refill_rate=1.0) is False

    # Fake time jump of 1.5 seconds → 1.5 tokens refilled → enough for 1.
    now = time.time()
    with patch("app.web.middleware.rate_limit.time") as mock_time:
        mock_time.time.return_value = now + 1.5
        assert check_and_consume(key, capacity=2, refill_rate=1.0) is True


def test_bypass_when_rate_limit_disabled(sqlite_tmp, monkeypatch):
    monkeypatch.setenv("RATE_LIMIT_ENABLED", "false")
    key = "test:bypass"
    # Exhaust normally.
    for _ in range(3):
        check_and_consume(key, capacity=3, refill_rate=0.0)
    # With bypass, the next call should be allowed regardless.
    assert check_and_consume(key, capacity=3, refill_rate=0.0) is True


def test_login_endpoint_returns_429(sqlite_tmp, monkeypatch):
    """10 login attempts from same IP are allowed; the 11th gets 429."""
    from fastapi.testclient import TestClient  # noqa: PLC0415

    from app.web.server import app  # noqa: PLC0415

    monkeypatch.setenv("RATE_LIMIT_ENABLED", "true")

    with TestClient(app, raise_server_exceptions=True) as client:
        body = {"email": "x@example.com", "password": "wrongpassword"}
        statuses = []
        for _ in range(12):
            r = client.post("/api/auth/login", json=body)
            statuses.append(r.status_code)

    # First 10: 401 (wrong creds), 11th and 12th: 429 (rate limited).
    assert statuses[:10] == [401] * 10
    assert 429 in statuses[10:]
