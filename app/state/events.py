"""Persisted side of the state machine: event log + projection + transition().

Every mutation of `imports.portal_status` / `imports.production_status` is:

    1. validated against the transition table in app.state.machine
    2. appended to `order_lifecycle_events` (the source of truth)
    3. projected into `imports` columns (the cached read model)

All in a single SQLite transaction. No partial writes.

`replay_state(import_id)` reconstructs the projection from scratch by
folding the event log. Used in property tests to detect drift.
"""
from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from app.observability.trace import current_trace_id
from app.persistence import db
from app.state.machine import (
    EventSource,
    InvalidTransitionError,
    LifecycleEvent,
    PortalStatus,
    ProductionStatus,
    apply_event,
)


@dataclass(frozen=True)
class TransitionResult:
    portal_status: PortalStatus
    production_status: ProductionStatus
    event_id: int
    state_version: int


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _read_current_state(
    conn: sqlite3.Connection, import_id: str
) -> tuple[PortalStatus, ProductionStatus, int] | None:
    row = conn.execute(
        "SELECT portal_status, production_status, state_version FROM imports WHERE id = ?",
        (import_id,),
    ).fetchone()
    if row is None:
        return None
    return (
        PortalStatus(row["portal_status"]),
        ProductionStatus(row["production_status"]),
        int(row["state_version"]),
    )


def _insert_event(
    conn: sqlite3.Connection,
    *,
    import_id: str,
    event_type: LifecycleEvent,
    source: EventSource,
    payload: dict | None,
    trace_id: str | None,
    occurred_at: str,
) -> int:
    cur = conn.execute(
        """
        INSERT INTO order_lifecycle_events
            (import_id, event_type, source, payload_json, trace_id,
             occurred_at, ingested_at)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (
            import_id,
            event_type.value,
            source.value,
            json.dumps(payload, ensure_ascii=False) if payload is not None else None,
            trace_id,
            occurred_at,
            _now(),
        ),
    )
    return int(cur.lastrowid)


def append_event(
    import_id: str,
    event: LifecycleEvent,
    *,
    source: EventSource = EventSource.PORTAL,
    payload: dict | None = None,
    trace_id: str | None = None,
    occurred_at: str | None = None,
) -> int:
    """Append a lifecycle event WITHOUT projecting state. Use sparingly —
    the canonical mutation path is `transition()`.

    Useful only for purely informational events that don't move either axis
    (rare). Returns the new event row id.
    """
    tid = trace_id if trace_id is not None else current_trace_id()
    occurred = occurred_at or _now()
    with db.connect() as conn:
        return _insert_event(
            conn,
            import_id=import_id,
            event_type=event,
            source=source,
            payload=payload,
            trace_id=tid,
            occurred_at=occurred,
        )


def transition(
    import_id: str,
    event: LifecycleEvent,
    *,
    source: EventSource = EventSource.PORTAL,
    payload: dict | None = None,
    trace_id: str | None = None,
    occurred_at: str | None = None,
    expected_state_version: int | None = None,
) -> TransitionResult:
    """The ONLY API that mutates portal_status / production_status.

    Steps (single transaction):
        1. Read current state + state_version.
        2. Compute next state via `apply_event` (raises on invalid).
        3. Insert event row.
        4. UPDATE imports projection columns + bump state_version.

    `expected_state_version` enables optimistic concurrency. If provided
    and it doesn't match, raises sqlite3.IntegrityError-equivalent
    (StaleStateError). Worker + UI updates use this to detect races.
    """
    tid = trace_id if trace_id is not None else current_trace_id()
    occurred = occurred_at or _now()

    with db.connect() as conn:
        snapshot = _read_current_state(conn, import_id)
        if snapshot is None:
            raise LookupError(f"import_id not found: {import_id}")
        portal, production, version = snapshot

        if expected_state_version is not None and expected_state_version != version:
            raise StaleStateError(
                import_id=import_id,
                expected=expected_state_version,
                actual=version,
            )

        try:
            new_portal, new_production = apply_event(portal, production, event)
        except InvalidTransitionError:
            raise

        event_id = _insert_event(
            conn,
            import_id=import_id,
            event_type=event,
            source=source,
            payload=payload,
            trace_id=tid,
            occurred_at=occurred,
        )

        new_version = version + 1
        conn.execute(
            """
            UPDATE imports
               SET portal_status     = ?,
                   production_status = ?,
                   state_version     = ?
             WHERE id = ?
            """,
            (new_portal.value, new_production.value, new_version, import_id),
        )

        return TransitionResult(
            portal_status=new_portal,
            production_status=new_production,
            event_id=event_id,
            state_version=new_version,
        )


class StaleStateError(Exception):
    """Optimistic concurrency violation: state_version mismatch."""

    def __init__(self, *, import_id: str, expected: int, actual: int) -> None:
        super().__init__(
            f"Stale state for {import_id}: expected version {expected}, found {actual}"
        )
        self.import_id = import_id
        self.expected = expected
        self.actual = actual


def list_events(import_id: str, limit: int = 500) -> list[dict[str, Any]]:
    """Return events ordered by occurred_at ASC (chronological — for replay)."""
    limit = max(1, min(int(limit), 5000))
    with db.connect() as conn:
        rows = conn.execute(
            """
            SELECT id, import_id, event_type, source, payload_json,
                   trace_id, occurred_at, ingested_at
              FROM order_lifecycle_events
             WHERE import_id = ?
             ORDER BY occurred_at ASC, id ASC
             LIMIT ?
            """,
            (import_id, limit),
        ).fetchall()
    return [
        {
            "id": r["id"],
            "import_id": r["import_id"],
            "event_type": r["event_type"],
            "source": r["source"],
            "payload": json.loads(r["payload_json"]) if r["payload_json"] else None,
            "trace_id": r["trace_id"],
            "occurred_at": r["occurred_at"],
            "ingested_at": r["ingested_at"],
        }
        for r in rows
    ]


def replay_state(import_id: str) -> tuple[PortalStatus, ProductionStatus]:
    """Fold the event log into a final (portal, production) state.

    Starts from PARSED/NONE (the implicit initial state of any pedido).
    Used in tests to verify the cached projection has not drifted.
    Skips events that don't match the current state — log can outlive a
    rejected transition (e.g. SEND_TO_FIRE_FAILED in a state where it's
    informational only).
    """
    portal: PortalStatus = PortalStatus.PARSED
    production: ProductionStatus = ProductionStatus.NONE

    with db.connect() as conn:
        rows = conn.execute(
            """
            SELECT event_type FROM order_lifecycle_events
             WHERE import_id = ?
             ORDER BY occurred_at ASC, id ASC
            """,
            (import_id,),
        ).fetchall()

    for r in rows:
        try:
            event = LifecycleEvent(r["event_type"])
        except ValueError:
            # Unknown event type (legacy / future) — ignore in replay
            continue
        try:
            portal, production = apply_event(portal, production, event)
        except InvalidTransitionError:
            # Event was logged but doesn't apply in this state — skip in replay
            continue

    return portal, production


__all__ = [
    "StaleStateError",
    "TransitionResult",
    "append_event",
    "list_events",
    "replay_state",
    "transition",
]
