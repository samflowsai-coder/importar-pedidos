"""Firebird status poll job — runs every 60s via the worker scheduler.

For every import that is `sent_to_fire` + `production_status=none` within
the last _WINDOW_DAYS days:
  1. Query CAB_VENDAS for the current STATUS.
  2. Stamp fire_status_last_seen + fire_status_polled_at on `imports`.
  3. If status changed, append FIRE_STATUS_CHANGED to the lifecycle log.
  4. If the status matches FIRE_TRIGGER_STATUS (env var), enqueue the order
     to the outbox and emit POST_TO_GESTOR_REQUESTED — the drain job will
     post it to Gestor de Produção on the next tick.

When FIRE_TRIGGER_STATUS is empty (default), steps 1–3 still run (observability)
but step 4 never fires. This is intentional: the trigger status must be
confirmed with the ERP client before enabling automation.
"""
from __future__ import annotations

import uuid
from datetime import UTC, datetime

from app import config as app_config
from app.erp.connection import FirebirdConnection
from app.erp.queries import GET_ORDER_STATUS_BY_CODE
from app.integrations.gestor.client import GESTOR_TARGET_NAME
from app.integrations.gestor.mapper import build_gestor_payload
from app.models.order import Order
from app.observability.trace import with_trace_id
from app.persistence import outbox_repo, repo
from app.persistence.outbox_repo import OutboxDuplicateError
from app.state.events import append_event, transition
from app.state.machine import EventSource, LifecycleEvent
from app.utils.logger import logger

_WINDOW_DAYS = 7


def run_poll_fire() -> None:
    """Poll Firebird for order status changes. No-op if Firebird is not configured."""
    fire_conn = FirebirdConnection()
    if not fire_conn.is_configured():
        return

    trigger = app_config.load().get("fire_trigger_status", "")
    pending = repo.list_pending_for_fire_poll(_WINDOW_DAYS)
    if not pending:
        return

    now_iso = datetime.now(UTC).isoformat()

    with fire_conn.connect() as conn:
        for entry in pending:
            fire_codigo = entry["fire_codigo"]
            row = conn.execute(GET_ORDER_STATUS_BY_CODE, (fire_codigo,)).fetchone()
            if not row:
                continue

            new_status: str = row["STATUS"]
            repo.update_fire_poll_result(entry["id"], new_status, now_iso)

            if new_status == entry["fire_status_last_seen"]:
                continue

            with with_trace_id(entry.get("trace_id")):
                append_event(
                    entry["id"],
                    LifecycleEvent.FIRE_STATUS_CHANGED,
                    source=EventSource.FIRE,
                    payload={
                        "fire_status": new_status,
                        "previous": entry["fire_status_last_seen"],
                    },
                    trace_id=entry.get("trace_id"),
                )
                logger.info(
                    "worker.poll import_id={} fire_codigo={} status={} prev={}",
                    entry["id"],
                    fire_codigo,
                    new_status,
                    entry["fire_status_last_seen"],
                )

                if trigger and new_status == trigger:
                    _enqueue_gestor(entry, trace_id=entry.get("trace_id"))


def _enqueue_gestor(entry: dict, *, trace_id: str | None) -> None:
    """Enqueue an outbox row for the drain job to post to Gestor."""
    snapshot_json = entry.get("snapshot_json")
    if not snapshot_json:
        logger.warning("worker.poll no snapshot import_id={}", entry["id"])
        return

    try:
        order = Order.model_validate_json(snapshot_json)
    except Exception:
        logger.exception("worker.poll snapshot parse error import_id={}", entry["id"])
        return

    payload_obj = build_gestor_payload(
        import_id=entry["id"],
        order=order,
        metadata={k: entry[k] for k in ("fire_codigo", "trace_id") if entry.get(k)},
    )
    payload_dict = payload_obj.model_dump()
    idempotency_key = str(uuid.uuid4())

    try:
        outbox_repo.enqueue(
            import_id=entry["id"],
            target=GESTOR_TARGET_NAME,
            endpoint="/v1/orders",
            payload=payload_dict,
            idempotency_key=idempotency_key,
            trace_id=trace_id,
        )
        transition(
            entry["id"],
            LifecycleEvent.POST_TO_GESTOR_REQUESTED,
            source=EventSource.FIRE,
            payload={
                "triggered_by": "fire_poll",
                "fire_status": entry.get("fire_status_last_seen"),
            },
            trace_id=trace_id,
        )
        logger.info(
            "worker.poll enqueued import_id={} trigger_status={}",
            entry["id"],
            entry.get("fire_status_last_seen"),
        )
    except OutboxDuplicateError:
        logger.warning("worker.poll outbox duplicate import_id={}", entry["id"])
    except Exception:
        logger.exception("worker.poll enqueue_failed import_id={}", entry["id"])
