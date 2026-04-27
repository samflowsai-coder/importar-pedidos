"""Outbox repository — durable queue for outbound integrations.

Workflow (Phase 3, manual trigger):
    1. Caller (server.py route) opens a SQLite txn:
         - `enqueue(import_id, target, endpoint, payload, idempotency_key)`
         - `app.state.transition(import_id, POST_TO_GESTOR_REQUESTED)`
    2. Caller drains inline (Phase 3) or worker drains (Phase 5):
         - `claim_next(target)` returns the oldest pending row whose
           `next_attempt_at` is due.
         - integration client POSTs.
         - on success: `mark_sent(row_id, response)` + transition SENT.
         - on failure: `mark_failed(row_id, error, backoff)` + transition FAILED.

Backoff schedule (worker, Phase 5): 30s, 2m, 10m, 1h, 6h, then `dead`.
For now (Phase 3 inline), we just `mark_failed` without rescheduling.

Idempotency: `idempotency_key` is UNIQUE in DB. Upstream callers use this
to keep replays safe — `enqueue` raises `OutboxDuplicateError` if reused.
"""
from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from app.observability.trace import current_trace_id
from app.persistence import db


class OutboxDuplicateError(Exception):
    """Raised when an idempotency_key already exists."""


@dataclass(frozen=True)
class OutboxRow:
    id: int
    import_id: str
    target: str
    endpoint: str
    payload: dict[str, Any]
    idempotency_key: str
    status: str
    attempts: int
    next_attempt_at: str | None
    last_error: str | None
    response: dict[str, Any] | None
    trace_id: str | None
    created_at: str
    sent_at: str | None


def _row_to_outbox(row: sqlite3.Row) -> OutboxRow:
    return OutboxRow(
        id=int(row["id"]),
        import_id=row["import_id"],
        target=row["target"],
        endpoint=row["endpoint"],
        payload=json.loads(row["payload_json"]),
        idempotency_key=row["idempotency_key"],
        status=row["status"],
        attempts=int(row["attempts"]),
        next_attempt_at=row["next_attempt_at"],
        last_error=row["last_error"],
        response=json.loads(row["response_json"]) if row["response_json"] else None,
        trace_id=row["trace_id"],
        created_at=row["created_at"],
        sent_at=row["sent_at"],
    )


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def enqueue(
    *,
    import_id: str,
    target: str,
    endpoint: str,
    payload: dict[str, Any],
    idempotency_key: str,
    trace_id: str | None = None,
) -> OutboxRow:
    """Insert a pending outbox row. Raises OutboxDuplicateError on key reuse."""
    tid = trace_id if trace_id is not None else current_trace_id()
    now = _now()
    payload_json = json.dumps(payload, ensure_ascii=False)
    try:
        with db.connect() as conn:
            cur = conn.execute(
                """
                INSERT INTO outbox (
                    import_id, target, endpoint, payload_json, idempotency_key,
                    status, attempts, next_attempt_at, last_error, response_json,
                    trace_id, created_at, sent_at
                ) VALUES (?, ?, ?, ?, ?, 'pending', 0, ?, NULL, NULL, ?, ?, NULL)
                """,
                (import_id, target, endpoint, payload_json, idempotency_key, now, tid, now),
            )
            new_id = int(cur.lastrowid)
            row = conn.execute(
                "SELECT * FROM outbox WHERE id = ?", (new_id,),
            ).fetchone()
        return _row_to_outbox(row)
    except sqlite3.IntegrityError as exc:
        if "idempotency_key" in str(exc).lower() or "UNIQUE" in str(exc):
            raise OutboxDuplicateError(
                f"idempotency_key already enqueued: {idempotency_key}"
            ) from exc
        raise


def get(row_id: int) -> OutboxRow | None:
    with db.connect() as conn:
        row = conn.execute("SELECT * FROM outbox WHERE id = ?", (row_id,)).fetchone()
    return _row_to_outbox(row) if row else None


def find_by_idempotency_key(key: str) -> OutboxRow | None:
    with db.connect() as conn:
        row = conn.execute(
            "SELECT * FROM outbox WHERE idempotency_key = ?", (key,)
        ).fetchone()
    return _row_to_outbox(row) if row else None


def list_for_import(import_id: str, limit: int = 100) -> list[OutboxRow]:
    limit = max(1, min(int(limit), 500))
    with db.connect() as conn:
        rows = conn.execute(
            """
            SELECT * FROM outbox
             WHERE import_id = ?
             ORDER BY created_at DESC, id DESC
             LIMIT ?
            """,
            (import_id, limit),
        ).fetchall()
    return [_row_to_outbox(r) for r in rows]


def claim_next(target: str, *, now: str | None = None) -> OutboxRow | None:
    """Return the oldest pending row for `target` whose next_attempt_at is due.

    Phase 3 calls this inline, single-process. Phase 5 worker will call it
    from a separate process — same query is correct because SQLite serializes
    writes; the consumer flips status to 'sending' if needed (not done here
    yet; inline drain is fast enough that a stale read is fine).
    """
    now = now or _now()
    with db.connect() as conn:
        row = conn.execute(
            """
            SELECT * FROM outbox
             WHERE target = ?
               AND status = 'pending'
               AND (next_attempt_at IS NULL OR next_attempt_at <= ?)
             ORDER BY created_at ASC, id ASC
             LIMIT 1
            """,
            (target, now),
        ).fetchone()
    return _row_to_outbox(row) if row else None


def mark_sent(
    row_id: int,
    *,
    response: dict[str, Any] | None = None,
) -> None:
    with db.connect() as conn:
        conn.execute(
            """
            UPDATE outbox
               SET status = 'sent',
                   sent_at = ?,
                   response_json = ?,
                   last_error = NULL,
                   next_attempt_at = NULL
             WHERE id = ?
            """,
            (
                _now(),
                json.dumps(response, ensure_ascii=False) if response is not None else None,
                row_id,
            ),
        )


def mark_failed(
    row_id: int,
    *,
    error: str,
    next_attempt_at: str | None = None,
    dead: bool = False,
) -> None:
    """Bump `attempts`, store `last_error`. If `dead=True`, stop retrying."""
    new_status = "dead" if dead else "pending"
    with db.connect() as conn:
        conn.execute(
            """
            UPDATE outbox
               SET status = ?,
                   attempts = attempts + 1,
                   last_error = ?,
                   next_attempt_at = ?
             WHERE id = ?
            """,
            (new_status, error[:1000], next_attempt_at, row_id),
        )


__all__ = [
    "OutboxDuplicateError",
    "OutboxRow",
    "claim_next",
    "enqueue",
    "find_by_idempotency_key",
    "get",
    "list_for_import",
    "mark_failed",
    "mark_sent",
]
