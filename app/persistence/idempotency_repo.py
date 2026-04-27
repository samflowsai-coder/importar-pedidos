"""Inbound webhook idempotency — dedup by (provider, event_id).

Why:
    External providers retry webhooks on network blips, restarts, or after
    timing out our response. The same logical event arrives N times. We
    must process it once and reply identically every time.

API:
    record_attempt(provider, event_id, *, import_id=None) -> Optional[CachedResponse]
        Returns None if first time (caller should process the event).
        Returns CachedResponse if already seen (caller short-circuits).

    finalize(provider, event_id, *, status, body)
        Stamp the response so future replays return the exact same answer.
        Idempotent (safe to call multiple times).

    list_for_import(import_id, limit=100) -> list of recent records (debug).

Design choice: we record the *attempt* (PRIMARY KEY insert) BEFORE
processing, so a concurrent retry hits a UNIQUE constraint and is rejected
cleanly by `record_attempt`. The first caller then runs to completion and
calls `finalize` to stamp the response.
"""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime

from app.persistence import db


@dataclass(frozen=True)
class CachedResponse:
    provider: str
    event_id: str
    received_at: str
    response_status: int | None
    response_body: str | None
    import_id: str | None


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def record_attempt(
    provider: str,
    event_id: str,
    *,
    import_id: str | None = None,
) -> CachedResponse | None:
    """Try to claim this (provider, event_id). Return None if first time.

    If a row already exists, return its current cached response — even if
    the original processing is still in flight (response_status NULL). The
    caller can decide to wait, return 202, or echo back the cached value.
    """
    try:
        with db.connect() as conn:
            conn.execute(
                """
                INSERT INTO inbound_idempotency
                    (provider, event_id, received_at, response_status,
                     response_body, import_id)
                VALUES (?, ?, ?, NULL, NULL, ?)
                """,
                (provider, event_id, _now(), import_id),
            )
        return None  # first time — caller processes
    except sqlite3.IntegrityError:
        # Already seen — return whatever we have (may still be in flight).
        return get(provider, event_id)


def get(provider: str, event_id: str) -> CachedResponse | None:
    with db.connect() as conn:
        row = conn.execute(
            """
            SELECT provider, event_id, received_at, response_status,
                   response_body, import_id
              FROM inbound_idempotency
             WHERE provider = ? AND event_id = ?
            """,
            (provider, event_id),
        ).fetchone()
    if row is None:
        return None
    return CachedResponse(
        provider=row["provider"],
        event_id=row["event_id"],
        received_at=row["received_at"],
        response_status=row["response_status"],
        response_body=row["response_body"],
        import_id=row["import_id"],
    )


def finalize(
    provider: str,
    event_id: str,
    *,
    status: int,
    body: str,
    import_id: str | None = None,
) -> None:
    """Stamp the final response. Safe to call multiple times.

    `import_id` overwrites the placeholder set in `record_attempt` if the
    correlation was determined later (e.g. lookup by gestor_order_id).
    """
    with db.connect() as conn:
        conn.execute(
            """
            UPDATE inbound_idempotency
               SET response_status = ?,
                   response_body   = ?,
                   import_id       = COALESCE(?, import_id)
             WHERE provider = ? AND event_id = ?
            """,
            (status, body[:2000], import_id, provider, event_id),
        )


def list_for_import(import_id: str, limit: int = 100) -> list[CachedResponse]:
    limit = max(1, min(int(limit), 500))
    with db.connect() as conn:
        rows = conn.execute(
            """
            SELECT provider, event_id, received_at, response_status,
                   response_body, import_id
              FROM inbound_idempotency
             WHERE import_id = ?
             ORDER BY received_at DESC
             LIMIT ?
            """,
            (import_id, limit),
        ).fetchall()
    return [
        CachedResponse(
            provider=r["provider"],
            event_id=r["event_id"],
            received_at=r["received_at"],
            response_status=r["response_status"],
            response_body=r["response_body"],
            import_id=r["import_id"],
        )
        for r in rows
    ]


__all__ = ["CachedResponse", "finalize", "get", "list_for_import", "record_attempt"]
