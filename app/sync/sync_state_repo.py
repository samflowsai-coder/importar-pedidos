"""Per-environment SQLite state for product sync.

All operations use the active environment's DB via `db.connect()`.
Functions are top-level (matches `outbox_repo.py`, `repo.py` patterns).
"""
from __future__ import annotations

import json
from datetime import UTC, datetime

from app.persistence import context as env_context
from app.persistence import db
from app.sync.models import RunResult, Trigger


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


# ── State load ──────────────────────────────────────────────────────────


def load_product_state() -> dict[int, str]:
    """Returns {seq: content_hash} for all known products in this env."""
    with db.connect() as conn:
        rows = conn.execute(
            "SELECT seq, content_hash FROM product_sync_state"
        ).fetchall()
    return {int(r["seq"]): r["content_hash"] for r in rows}


def load_component_state() -> dict[int, str]:
    """Returns {codigo: content_hash} for all known components in this env."""
    with db.connect() as conn:
        rows = conn.execute(
            "SELECT codigo, content_hash FROM component_sync_state"
        ).fetchall()
    return {int(r["codigo"]): r["content_hash"] for r in rows}


# ── State commit ────────────────────────────────────────────────────────


def commit_states(
    *,
    product_upserts: dict[int, str],
    product_tombstones: list[int],
    component_upserts: dict[int, str],
    component_tombstones: list[int],
) -> None:
    """Atomically applies all state changes in a single transaction."""
    now = _now()
    with db.connect() as conn:
        for seq, h in product_upserts.items():
            conn.execute(
                """INSERT INTO product_sync_state (seq, content_hash, last_synced_at)
                   VALUES (?, ?, ?)
                   ON CONFLICT(seq) DO UPDATE SET
                     content_hash = excluded.content_hash,
                     last_synced_at = excluded.last_synced_at""",
                (seq, h, now),
            )
        for seq in product_tombstones:
            conn.execute("DELETE FROM product_sync_state WHERE seq = ?", (seq,))

        for codigo, h in component_upserts.items():
            conn.execute(
                """INSERT INTO component_sync_state (codigo, content_hash, last_synced_at)
                   VALUES (?, ?, ?)
                   ON CONFLICT(codigo) DO UPDATE SET
                     content_hash = excluded.content_hash,
                     last_synced_at = excluded.last_synced_at""",
                (codigo, h, now),
            )
        for codigo in component_tombstones:
            conn.execute("DELETE FROM component_sync_state WHERE codigo = ?", (codigo,))


# ── Run records ─────────────────────────────────────────────────────────


def record_run_start(*, sync_id: str, trigger: Trigger, trace_id: str | None) -> None:
    """Insert a `'running'` row into product_sync_runs.

    If the process crashes between `record_run_start` and `record_run_finish`,
    the row remains as `'running'` indefinitely. Today this is harmless because
    `consecutive_failure_count` only inspects `'failed'` rows. Future readers
    that count `'running'` rows must handle this case (e.g., treat any
    `'running'` row older than N minutes as stale).
    """
    cur = env_context.current()
    if cur is None:
        raise RuntimeError("record_run_start: no active environment")
    with db.connect() as conn:
        conn.execute(
            """INSERT INTO product_sync_runs
                 (environment_id, sync_id, trigger, started_at, status, trace_id)
               VALUES (?, ?, ?, ?, 'running', ?)""",
            (cur["id"], sync_id, trigger.value, _now(), trace_id),
        )


def record_run_finish(*, sync_id: str, result: RunResult) -> None:
    """Update an existing run row with final status, counters, and any errors."""
    errors_json = json.dumps([e.model_dump() for e in result.errors]) if result.errors else None
    with db.connect() as conn:
        conn.execute(
            """UPDATE product_sync_runs SET
                 finished_at = ?,
                 status = ?,
                 delta_count_produtos = ?,
                 delta_count_componentes = ?,
                 delta_count_tombstones = ?,
                 applied_count = ?,
                 errors_json = ?
               WHERE sync_id = ?""",
            (
                _now(),
                result.status.value,
                result.delta_count_produtos,
                result.delta_count_componentes,
                result.delta_count_tombstones,
                result.applied_count,
                errors_json,
                sync_id,
            ),
        )


def list_runs(*, limit: int = 50) -> list[dict]:
    with db.connect() as conn:
        rows = conn.execute(
            """SELECT * FROM product_sync_runs
               ORDER BY started_at DESC LIMIT ?""",
            (limit,),
        ).fetchall()
    return [dict(r) for r in rows]


def consecutive_failure_count() -> int:
    """Counts consecutive 'failed' runs from the most recent backwards.

    Inspects up to the last 20 runs only — sufficient for any practical
    circuit-breaker threshold (we open the circuit at 5 consecutive failures).
    If a circuit-breaker threshold ever exceeds 20, raise the LIMIT here.

    Used by the circuit breaker.
    """
    with db.connect() as conn:
        rows = conn.execute(
            "SELECT status FROM product_sync_runs ORDER BY started_at DESC, id DESC LIMIT 20"
        ).fetchall()
    count = 0
    for r in rows:
        if r["status"] == "failed":
            count += 1
        else:
            break
    return count


__all__ = [
    "commit_states",
    "consecutive_failure_count",
    "list_runs",
    "load_component_state",
    "load_product_state",
    "record_run_finish",
    "record_run_start",
]
