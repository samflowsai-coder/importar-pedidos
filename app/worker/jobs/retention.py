"""Retention and backup job (Fase 6).

Scheduled daily at 03:00 by app/worker/scheduler.py.

What it does:
1. Purges old order_lifecycle_events  (> RETENTION_DAYS, default 180)
2. Purges old audit_log entries       (> RETENTION_DAYS)
3. Purges old inbound_idempotency     (> 90 days — shorter TTL, dedup window)
4. Deletes expired sessions           (expires_at < now)
5. Removes stale rate_limit_buckets   (inactive for > 1 day)
6. VACUUM INTO daily backup           (only if BACKUP_DIR env var is set)
   — keeps the last 7 backup files, removes older ones
"""
from __future__ import annotations

import sqlite3
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

from app import config as app_config
from app.persistence.db import connect, db_path
from app.utils.logger import logger

_IDEMPOTENCY_TTL_DAYS = 90
_RATE_LIMIT_STALE_SECONDS = 86_400  # 1 day
_BACKUP_KEEP = 7


def _utc_cutoff(days: int) -> str:
    """ISO-8601 timestamp for *days* ago (UTC)."""
    return (datetime.now(UTC) - timedelta(days=days)).isoformat()


def _purge_lifecycle_events(conn: sqlite3.Connection, cutoff: str) -> int:
    cur = conn.execute(
        "DELETE FROM order_lifecycle_events WHERE occurred_at < ?", (cutoff,)
    )
    return cur.rowcount


def _purge_audit_log(conn: sqlite3.Connection, cutoff: str) -> int:
    cur = conn.execute("DELETE FROM audit_log WHERE created_at < ?", (cutoff,))
    return cur.rowcount


def _purge_idempotency(conn: sqlite3.Connection) -> int:
    cutoff = _utc_cutoff(_IDEMPOTENCY_TTL_DAYS)
    cur = conn.execute(
        "DELETE FROM inbound_idempotency WHERE received_at < ?", (cutoff,)
    )
    return cur.rowcount


def _purge_expired_sessions(conn: sqlite3.Connection) -> int:
    now = datetime.now(UTC).isoformat()
    cur = conn.execute("DELETE FROM sessions WHERE expires_at < ?", (now,))
    return cur.rowcount


def _purge_stale_rate_limit_buckets(conn: sqlite3.Connection) -> int:
    import time  # noqa: PLC0415

    stale_before = time.time() - _RATE_LIMIT_STALE_SECONDS
    cur = conn.execute(
        "DELETE FROM rate_limit_buckets WHERE last_refill_at < ?", (stale_before,)
    )
    return cur.rowcount


def _vacuum_backup(backup_dir: str) -> None:
    dest_dir = Path(backup_dir)
    try:
        dest_dir.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        logger.error("retention.backup mkdir_failed dir={} error={}", backup_dir, exc)
        return

    dest = dest_dir / f"app_state_{date.today():%Y%m%d}.db"

    try:
        # VACUUM INTO is safe while the DB is in use — SQLite acquires a shared
        # read lock and writes to the destination file atomically.
        src = str(db_path())
        conn = sqlite3.connect(src, timeout=10.0)
        try:
            conn.execute(f"VACUUM INTO '{dest}'")
        finally:
            conn.close()
        logger.info("retention.backup_created path={}", dest)
    except Exception as exc:
        logger.error("retention.backup_failed dest={} error={}", dest, exc)
        return

    # Keep only the N most recent backup files.
    backups = sorted(dest_dir.glob("app_state_????????.db"))
    to_remove = backups[: max(0, len(backups) - _BACKUP_KEEP)]
    for old in to_remove:
        try:
            old.unlink(missing_ok=True)
            logger.debug("retention.backup_removed path={}", old)
        except OSError as exc:
            logger.warning("retention.backup_remove_failed path={} error={}", old, exc)


def run_retention() -> None:
    """Entry point called by APScheduler."""
    cfg = app_config.load()
    retention_days: int = cfg.get("retention_days", 180)
    backup_dir: str | None = cfg.get("backup_dir")

    cutoff = _utc_cutoff(retention_days)
    logger.info("retention.start retention_days={} cutoff={}", retention_days, cutoff)

    try:
        with connect() as conn:
            events = _purge_lifecycle_events(conn, cutoff)
            audit = _purge_audit_log(conn, cutoff)
            idempotency = _purge_idempotency(conn)
            sessions = _purge_expired_sessions(conn)
            buckets = _purge_stale_rate_limit_buckets(conn)

        logger.info(
            "retention.purged lifecycle_events={} audit_log={} idempotency={} "
            "sessions={} rate_limit_buckets={}",
            events, audit, idempotency, sessions, buckets,
        )
    except Exception as exc:
        logger.error("retention.purge_failed error={}", exc)
        return

    if backup_dir:
        _vacuum_backup(backup_dir)
    else:
        logger.debug("retention.backup_skipped reason=BACKUP_DIR_not_set")
