"""Tests for app.worker.jobs.retention (purge + VACUUM INTO backup)."""
from __future__ import annotations

import time
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from app.persistence import db, repo
from app.persistence.db import connect, connect_shared
from app.worker.jobs.retention import (
    _purge_audit_log,
    _purge_expired_sessions,
    _purge_lifecycle_events,
    _purge_stale_rate_limit_buckets,
    _vacuum_backup,
    run_retention,
)


@pytest.fixture
def sqlite_tmp(tmp_path: Path):
    db.set_db_path(tmp_path / "app_state.db")
    db.reset_init_cache()
    db.init()
    yield tmp_path
    db.set_db_path(None)
    db.reset_init_cache()


def _iso(days_ago: int) -> str:
    return (datetime.now(UTC) - timedelta(days=days_ago)).isoformat()


def _seed_import() -> str:
    iid = str(uuid.uuid4())
    repo.insert_import({
        "id": iid,
        "source_filename": "p.pdf",
        "imported_at": datetime.now().isoformat(timespec="seconds"),
        "snapshot": {"header": {"order_number": "X"}, "items": []},
        "status": "success",
        "portal_status": "sent_to_fire",
        "fire_codigo": 99,
    })
    return iid


# ── lifecycle events ──────────────────────────────────────────────────────

def test_purge_lifecycle_events_removes_old_rows(sqlite_tmp):
    iid = _seed_import()
    with connect() as conn:
        conn.execute(
            "INSERT INTO order_lifecycle_events "
            "(environment_id, import_id, event_type, source, occurred_at, ingested_at) "
            "VALUES ('test-env-id', ?, 'TEST', 'PORTAL', ?, ?)",
            (iid, _iso(200), _iso(200)),
        )
        conn.execute(
            "INSERT INTO order_lifecycle_events "
            "(environment_id, import_id, event_type, source, occurred_at, ingested_at) "
            "VALUES ('test-env-id', ?, 'TEST', 'PORTAL', ?, ?)",
            (iid, _iso(10), _iso(10)),
        )

    with connect() as conn:
        deleted = _purge_lifecycle_events(conn, _iso(180))

    assert deleted == 1
    with connect() as conn:
        remaining = conn.execute(
            "SELECT COUNT(*) FROM order_lifecycle_events WHERE import_id = ?", (iid,)
        ).fetchone()[0]
    assert remaining == 1


# ── audit_log ──────────────────────────────────────────────────────────────

def test_purge_audit_log_removes_old_rows(sqlite_tmp):
    iid = _seed_import()
    with connect() as conn:
        conn.execute(
            "INSERT INTO audit_log (environment_id, import_id, event_type, created_at) "
            "VALUES ('test-env-id', ?, 'E', ?)",
            (iid, _iso(200)),
        )
        conn.execute(
            "INSERT INTO audit_log (environment_id, import_id, event_type, created_at) "
            "VALUES ('test-env-id', ?, 'E', ?)",
            (iid, _iso(5)),
        )

    with connect() as conn:
        deleted = _purge_audit_log(conn, _iso(180))

    assert deleted == 1


# ── sessions ──────────────────────────────────────────────────────────────

def test_purge_expired_sessions(sqlite_tmp):
    with connect_shared() as conn:
        # Seed a user first (FK constraint).
        conn.execute(
            "INSERT INTO users (email, password_hash, role, active, created_at) "
            "VALUES ('u@t.com', 'x', 'admin', 1, ?)",
            (_iso(0),),
        )
        uid = conn.execute("SELECT id FROM users WHERE email='u@t.com'").fetchone()[0]

        conn.execute(
            "INSERT INTO sessions (token, user_id, created_at, expires_at) "
            "VALUES ('expired_tok', ?, ?, ?)",
            (uid, _iso(10), _iso(3)),
        )
        conn.execute(
            "INSERT INTO sessions (token, user_id, created_at, expires_at) "
            "VALUES ('valid_tok', ?, ?, ?)",
            (uid, _iso(1), (datetime.now(UTC) + timedelta(days=7)).isoformat()),
        )

    with connect_shared() as conn:
        deleted = _purge_expired_sessions(conn)

    assert deleted == 1


# ── rate_limit_buckets ────────────────────────────────────────────────────

def test_purge_stale_rate_limit_buckets(sqlite_tmp):
    stale_ts = time.time() - 90_000  # 25 h ago
    fresh_ts = time.time() - 1_800   # 30 min ago

    with connect_shared() as conn:
        conn.execute(
            "INSERT INTO rate_limit_buckets (key, tokens, last_refill_at) VALUES (?, 5, ?)",
            ("stale", stale_ts),
        )
        conn.execute(
            "INSERT INTO rate_limit_buckets (key, tokens, last_refill_at) VALUES (?, 5, ?)",
            ("fresh", fresh_ts),
        )

    with connect_shared() as conn:
        deleted = _purge_stale_rate_limit_buckets(conn)

    assert deleted == 1
    with connect_shared() as conn:
        remaining_keys = [
            r[0] for r in conn.execute("SELECT key FROM rate_limit_buckets").fetchall()
        ]
    assert remaining_keys == ["fresh"]


# ── VACUUM INTO backup ────────────────────────────────────────────────────

def test_vacuum_backup_creates_file(sqlite_tmp):
    backup_dir = sqlite_tmp / "backups"
    _vacuum_backup(str(backup_dir))

    # Multi-ambiente: backup gera 1 arquivo por DB (shared + 1 por env).
    # No fixture sqlite_tmp, não há environments cadastrados, então só
    # app_shared.db é backupeado.
    files = list(backup_dir.glob("app_*_????????.db"))
    assert len(files) == 1, f"Expected 1 backup file, got {files}"
    assert files[0].name.startswith("app_shared_")


def test_vacuum_backup_keeps_last_7(sqlite_tmp, tmp_path):
    backup_dir = tmp_path / "bkp"
    backup_dir.mkdir()
    # Pre-create 10 fake app_shared backups (mesmo "stem" do que vai ser gerado).
    for i in range(10):
        (backup_dir / f"app_shared_2025010{i:01d}.db").write_bytes(b"")

    _vacuum_backup(str(backup_dir))

    files = sorted(backup_dir.glob("app_shared_????????.db"))
    # 7 antigos + 1 novo = 8 máximo antes de podar o mais antigo.
    # Após poda: 7 mais recentes mantidos.
    assert len(files) == 7


def test_no_error_on_empty_db(sqlite_tmp, monkeypatch):
    monkeypatch.setenv("RETENTION_DAYS", "180")
    monkeypatch.delenv("BACKUP_DIR", raising=False)
    run_retention()  # should not raise
