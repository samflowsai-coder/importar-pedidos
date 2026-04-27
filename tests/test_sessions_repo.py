"""Tests for app.persistence.sessions_repo."""
from __future__ import annotations

import time
from pathlib import Path

import pytest

from app.persistence import db, sessions_repo, users_repo


@pytest.fixture
def sqlite_tmp(tmp_path: Path):
    db.set_db_path(tmp_path / "app_state.db")
    db.reset_init_cache()
    db.init()
    yield
    db.set_db_path(None)
    db.reset_init_cache()


def _make_user() -> int:
    u = users_repo.create_user(email="user@x.com", password="strongpass1")
    return u.id


def test_token_is_high_entropy():
    """Tokens are 32-byte URL-safe base64 — should be 43+ chars."""
    t1 = sessions_repo.new_token()
    t2 = sessions_repo.new_token()
    assert len(t1) >= 32
    assert t1 != t2


def test_create_and_get_active(sqlite_tmp):
    uid = _make_user()
    sess = sessions_repo.create_session(user_id=uid, ip="127.0.0.1")
    assert sess.token
    found = sessions_repo.get_active(sess.token)
    assert found is not None
    assert found.user_id == uid
    assert found.ip == "127.0.0.1"


def test_get_inactive_returns_none_for_unknown(sqlite_tmp):
    assert sessions_repo.get_active("nonexistent-token") is None
    assert sessions_repo.get_active("") is None


def test_expired_session_lazily_deleted(sqlite_tmp):
    uid = _make_user()
    # TTL=0 hours but timing may give us a 0-hour-equivalent that expires now
    # Use a very short TTL via direct DB write to be deterministic.
    from datetime import datetime, timedelta
    past = (datetime.now() - timedelta(hours=1)).isoformat(timespec="seconds")
    with db.connect() as conn:
        conn.execute(
            """
            INSERT INTO sessions (token, user_id, created_at, expires_at, ip, user_agent)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            ("expired-tok", uid, past, past, None, None),
        )
    assert sessions_repo.get_active("expired-tok") is None
    # And it was deleted
    with db.connect() as conn:
        row = conn.execute(
            "SELECT * FROM sessions WHERE token = ?", ("expired-tok",)
        ).fetchone()
    assert row is None


def test_delete_session(sqlite_tmp):
    uid = _make_user()
    sess = sessions_repo.create_session(user_id=uid)
    assert sessions_repo.get_active(sess.token) is not None
    sessions_repo.delete(sess.token)
    assert sessions_repo.get_active(sess.token) is None


def test_delete_all_for_user(sqlite_tmp):
    uid = _make_user()
    sessions_repo.create_session(user_id=uid)
    sessions_repo.create_session(user_id=uid)
    sessions_repo.create_session(user_id=uid)
    deleted = sessions_repo.delete_all_for_user(uid)
    assert deleted == 3


def test_cascade_delete_on_user(sqlite_tmp):
    uid = _make_user()
    sess = sessions_repo.create_session(user_id=uid)
    with db.connect() as conn:
        conn.execute("DELETE FROM users WHERE id = ?", (uid,))
    # Cascade FK
    assert sessions_repo.get_active(sess.token) is None


def test_prune_expired(sqlite_tmp):
    uid = _make_user()
    from datetime import datetime, timedelta
    past = (datetime.now() - timedelta(hours=1)).isoformat(timespec="seconds")
    with db.connect() as conn:
        conn.execute(
            "INSERT INTO sessions (token, user_id, created_at, expires_at) "
            "VALUES (?, ?, ?, ?)", ("e1", uid, past, past),
        )
        conn.execute(
            "INSERT INTO sessions (token, user_id, created_at, expires_at) "
            "VALUES (?, ?, ?, ?)", ("e2", uid, past, past),
        )
    sessions_repo.create_session(user_id=uid)  # active
    pruned = sessions_repo.prune_expired()
    assert pruned == 2


def test_user_agent_truncated(sqlite_tmp):
    uid = _make_user()
    huge_ua = "x" * 1000
    sess = sessions_repo.create_session(user_id=uid, user_agent=huge_ua)
    # Stored truncated to 500
    with db.connect() as conn:
        row = conn.execute(
            "SELECT user_agent FROM sessions WHERE token = ?", (sess.token,)
        ).fetchone()
    assert len(row["user_agent"]) == 500


def test_ttl_in_hours(sqlite_tmp):
    """TTL should produce expires_at roughly in the future by `ttl_hours`."""
    uid = _make_user()
    sess = sessions_repo.create_session(user_id=uid, ttl_hours=1)
    from datetime import datetime
    expires = datetime.fromisoformat(sess.expires_at)
    delta_seconds = (expires - datetime.now()).total_seconds()
    # Should be roughly 1h ± 5s
    assert 3590 < delta_seconds < 3610
    _ = time  # silence unused import; we keep `import time` for future tests
