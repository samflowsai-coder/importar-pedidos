"""Outbox drain job — runs every 15s via the worker scheduler.

Picks up to _BATCH pending rows from the outbox and posts each to the
Gestor de Produção API. On success, stamps the sent state and emits
POST_TO_GESTOR_SENT. On failure, applies exponential backoff; after
_BACKOFF_S is exhausted the row is marked dead and an informational
POST_TO_GESTOR_FAILED event is appended.

Backoff schedule (row.attempts is the count *before* this attempt):
  attempts=0 → retry in  30s
  attempts=1 → retry in   2min
  attempts=2 → retry in  10min
  attempts=3 → retry in   1h
  attempts=4 → retry in   6h
  attempts=5 → dead
"""

from __future__ import annotations

from datetime import datetime, timedelta

from pydantic import ValidationError

from app.integrations.flowpcp.client import FlowPCPClient, FlowPCPClientError
from app.integrations.flowpcp.config import FlowPCPConfig, enabled_flowpcp_envs
from app.integrations.flowpcp.schema import RecebimentoRequest
from app.integrations.gestor.client import (
    GESTOR_TARGET_NAME,
    GestorClient,
    GestorClientError,
)
from app.integrations.gestor.schema import GestorOrderRequest
from app.observability.metrics import update_outbox_metrics
from app.observability.trace import with_trace_id
from app.persistence import context as env_context
from app.persistence import environments_repo, outbox_repo, repo, router
from app.persistence.outbox_repo import OutboxRow
from app.state.events import append_event, transition
from app.state.machine import EventSource, LifecycleEvent
from app.utils.logger import logger

_BACKOFF_S = [30, 120, 600, 3600, 21600]  # 30s, 2m, 10m, 1h, 6h
_BATCH = 20  # max rows per invocation to avoid blocking the thread indefinitely


def run_drain_outbox() -> None:
    """Drain pending outbox rows em CADA ambiente ativo (até _BATCH por env)."""
    flowpcp_envs = enabled_flowpcp_envs()
    for slug in router.list_env_slugs():
        env = environments_repo.get_by_slug(slug)
        if env is None:
            continue
        with env_context.active_env(env["id"], env["slug"]):
            for _ in range(_BATCH):
                row = outbox_repo.claim_next(target=GESTOR_TARGET_NAME)
                if row is None:
                    break
                _process_row(row)

            cfg = flowpcp_envs.get(slug)
            if cfg and cfg.enabled:
                # Mesma regra da thread do processo web: triagem pelo ERP
                # (encerrado não vai), retenção da acumulada sem código.
                from app.integrations.flowpcp import envio_portal  # noqa: PLC0415 — ciclo de import

                try:
                    envio_portal.drenar_ambiente(env, cfg)
                except Exception as exc:  # noqa: BLE001 — um ambiente ruim não derruba os outros
                    logger.error("worker.drain flowpcp env={} falhou: {!r}", slug, exc)
    update_outbox_metrics()


def _process_flowpcp_row(row: OutboxRow, cfg: FlowPCPConfig) -> None:
    """Reenvia ao FlowPCP um pedido cujo send_order inline falhou e foi enfileirado."""
    with with_trace_id(row.trace_id):
        try:
            client = FlowPCPClient(
                base_url=cfg.base_url,
                service_token=cfg.service_token,
                tenant_id=cfg.tenant_id,
                timeout=cfg.request_timeout_s,
            )
            try:
                request = RecebimentoRequest.model_validate(row.payload)
                resp = client.send_order(request, idempotency_key=row.idempotency_key)
            finally:
                client.close()
            outbox_repo.mark_sent(row.id, response=resp)
            logger.info(
                "worker.drain flowpcp sent import_id={} idem={}",
                row.import_id,
                row.idempotency_key,
            )
        except Exception as exc:  # noqa: BLE001 — falha vira backoff/dead, não derruba o lote
            _handle_flowpcp_failure(row, str(exc), definitivo=_recusa_definitiva(exc))


def _recusa_definitiva(exc: Exception) -> bool:
    """Reenviar o mesmo corpo daria a mesma resposta: payload inválido aqui ou
    4xx de conteúdo do Flow. Credencial (401/403), timeout (408) e limite (429)
    são do ambiente, não da linha: seguem tentando."""
    if isinstance(exc, ValidationError):
        return True
    status = getattr(exc, "status_code", None) if isinstance(exc, FlowPCPClientError) else None
    return status is not None and 400 <= status < 500 and status not in (401, 403, 408, 429)


def _proxima_tentativa(delta_s: int) -> str:
    """Mesmo relógio e formato de `outbox_repo._now()` (hora local, segundos):
    `list_due`/`claim_next` comparam por texto. Em UTC com offset, 30 s viravam
    3 h 30 s no Brasil."""
    return (datetime.now() + timedelta(seconds=delta_s)).isoformat(timespec="seconds")


def _handle_flowpcp_failure(row: OutboxRow, error: str, *, definitivo: bool = False) -> None:
    if definitivo:
        outbox_repo.mark_failed(row.id, error=f"recusado: {error}", dead=True)
        logger.error(
            "worker.drain flowpcp recusado import_id={} error={}", row.import_id, error[:200]
        )
        return
    # Pedido nunca morre por falha de envio: Flow fora do ar ou credencial
    # errada por um dia não pode apagar a fila. Passado o escalonamento, tenta
    # a cada 6 h e grita no log.
    delta = _BACKOFF_S[min(row.attempts, len(_BACKOFF_S) - 1)]
    outbox_repo.mark_failed(row.id, error=error, next_attempt_at=_proxima_tentativa(delta))
    log = logger.error if row.attempts >= len(_BACKOFF_S) else logger.warning
    log(
        "worker.drain flowpcp retry import_id={} attempts={} next=+{}s error={}",
        row.import_id,
        row.attempts + 1,
        delta,
        error[:200],
    )


def _process_row(row: OutboxRow) -> None:
    with with_trace_id(row.trace_id):
        try:
            client = GestorClient()
            try:
                request = GestorOrderRequest(**row.payload)
                resp = client.create_order(request, idempotency_key=row.idempotency_key)
            finally:
                client.close()

            outbox_repo.mark_sent(row.id, response=resp.model_dump())
            repo.set_gestor_order_id(row.import_id, resp.id)
            transition(
                row.import_id,
                LifecycleEvent.POST_TO_GESTOR_SENT,
                source=EventSource.SYSTEM,
                payload={"gestor_order_id": resp.id},
                trace_id=row.trace_id,
            )
            logger.info(
                "worker.drain sent import_id={} gestor_order_id={}",
                row.import_id,
                resp.id,
            )

        except (GestorClientError, Exception) as exc:
            _handle_failure(row, str(exc))


def _handle_failure(row: OutboxRow, error: str) -> None:
    if row.attempts >= len(_BACKOFF_S):
        outbox_repo.mark_failed(row.id, error=error, dead=True)
        append_event(
            row.import_id,
            LifecycleEvent.POST_TO_GESTOR_FAILED,
            source=EventSource.SYSTEM,
            payload={"error": error, "attempts": row.attempts + 1},
            trace_id=row.trace_id,
        )
        logger.error(
            "worker.drain dead import_id={} attempts={}",
            row.import_id,
            row.attempts + 1,
        )
    else:
        delta = _BACKOFF_S[row.attempts]
        outbox_repo.mark_failed(row.id, error=error, next_attempt_at=_proxima_tentativa(delta))
        logger.warning(
            "worker.drain retry import_id={} attempts={} next=+{}s",
            row.import_id,
            row.attempts + 1,
            delta,
        )
