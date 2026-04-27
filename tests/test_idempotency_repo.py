"""Tests for app.persistence.idempotency_repo (inbound webhook dedup)."""
from __future__ import annotations

from pathlib import Path

import pytest

from app.persistence import db, idempotency_repo


@pytest.fixture
def sqlite_tmp(tmp_path: Path):
    db.set_db_path(tmp_path / "app_state.db")
    db.reset_init_cache()
    db.init()
    yield
    db.set_db_path(None)
    db.reset_init_cache()


def test_record_attempt_first_time_returns_none(sqlite_tmp):
    assert idempotency_repo.record_attempt("gestor", "evt-1") is None


def test_record_attempt_second_time_returns_cached(sqlite_tmp):
    first = idempotency_repo.record_attempt("gestor", "evt-1")
    second = idempotency_repo.record_attempt("gestor", "evt-1")
    assert first is None
    assert second is not None
    assert second.event_id == "evt-1"
    assert second.provider == "gestor"
    # Not finalized yet — response_status is None
    assert second.response_status is None


def test_finalize_stamps_response(sqlite_tmp):
    idempotency_repo.record_attempt("gestor", "evt-2")
    idempotency_repo.finalize(
        "gestor", "evt-2", status=200, body='{"ok": true}', import_id="imp-x",
    )
    cached = idempotency_repo.record_attempt("gestor", "evt-2")  # replay
    assert cached.response_status == 200
    assert cached.response_body == '{"ok": true}'
    assert cached.import_id == "imp-x"


def test_finalize_is_idempotent(sqlite_tmp):
    idempotency_repo.record_attempt("gestor", "evt-3")
    idempotency_repo.finalize("gestor", "evt-3", status=200, body="first")
    idempotency_repo.finalize("gestor", "evt-3", status=200, body="second")
    cached = idempotency_repo.get("gestor", "evt-3")
    assert cached.response_body == "second"  # last write wins, no error


def test_provider_isolation(sqlite_tmp):
    """Same event_id under different providers is independent."""
    assert idempotency_repo.record_attempt("gestor", "evt-shared") is None
    assert idempotency_repo.record_attempt("apontae", "evt-shared") is None  # different provider


def test_finalize_preserves_existing_import_id_when_passing_none(sqlite_tmp):
    idempotency_repo.record_attempt("gestor", "evt-4", import_id="imp-original")
    idempotency_repo.finalize("gestor", "evt-4", status=200, body="ok", import_id=None)
    cached = idempotency_repo.get("gestor", "evt-4")
    assert cached.import_id == "imp-original"


def test_response_body_truncated_to_2000_chars(sqlite_tmp):
    huge = "x" * 5000
    idempotency_repo.record_attempt("gestor", "evt-5")
    idempotency_repo.finalize("gestor", "evt-5", status=200, body=huge)
    cached = idempotency_repo.get("gestor", "evt-5")
    assert len(cached.response_body) == 2000


def test_get_returns_none_for_unknown(sqlite_tmp):
    assert idempotency_repo.get("gestor", "never-seen") is None
