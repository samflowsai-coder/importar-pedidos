"""pytest fixtures and global config.

Auth bypass for legacy tests:
    Phase 4b introduced session auth on mutation routes. The pre-existing
    tests for those routes (preview/commit/send-to-fire/cancel/etc.) don't
    care about the auth flow — they exercise the *underlying* behavior.
    Setting `TEST_AUTH_BYPASS=1` makes `require_user` return a synthetic
    test admin without going through the cookie/login dance.

    New tests that DO want to exercise the real auth flow (login attempts,
    cookie validation, session expiry) explicitly unset the bypass via the
    `real_auth` fixture below.
"""
from __future__ import annotations

import os
import sqlite3
from pathlib import Path

import pytest


def pytest_configure(config):  # noqa: ARG001 — pytest hook signature
    # Set BEFORE any app import resolves env. Tests that need real auth
    # use the `real_auth` fixture to override.
    os.environ.setdefault("TEST_AUTH_BYPASS", "1")
    # Cookies are sent over HTTP in the test client; mark cookie non-secure.
    os.environ.setdefault("PORTAL_COOKIE_SECURE", "0")


@pytest.fixture
def real_auth(monkeypatch):
    """Disable auth bypass — caller will exercise the real login flow."""
    monkeypatch.delenv("TEST_AUTH_BYPASS", raising=False)
    yield


@pytest.fixture
def tmp_shared_db(tmp_path: Path):
    """Empty SQLite for shared-DB schema/repo tests (future app_shared.db)."""
    db_file = tmp_path / "app_shared.db"
    conn = sqlite3.connect(db_file, isolation_level="DEFERRED")
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    yield conn
    conn.close()


@pytest.fixture
def tmp_env_db(tmp_path: Path):
    """Empty SQLite for per-env schema/repo tests (future app_state_<slug>.db)."""
    db_file = tmp_path / "app_state_test.db"
    conn = sqlite3.connect(db_file, isolation_level="DEFERRED")
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    yield conn
    conn.close()
