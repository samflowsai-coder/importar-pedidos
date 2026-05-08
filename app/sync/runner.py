"""Orchestrate a single product sync run for one environment.

Public API: `run(env, trigger) -> RunResult`.

Caller is responsible for fetching `env` from `environments_repo` and passing
it in. The runner activates the environment context internally to make
per-env DB writes (state, run records) work.
"""
from __future__ import annotations

import time
import uuid
from typing import Any

from app.integrations.flowpcp.client import FlowPCPClient, FlowPCPClientError
from app.observability import metrics
from app.persistence import environments_repo
from app.persistence.context import active_env
from app.sync import sync_state_repo
from app.sync.canonical import canonical_hash
from app.sync.diff_engine import compute_delta
from app.sync.fire_reader import (
    read_components_snapshot,
    read_products_snapshot,
)
from app.sync.models import (
    RunResult,
    RunStatus,
    SyncError,
    Trigger,
)
from app.utils.logger import logger

_FAILURE_THRESHOLD = 5


def _new_sync_id() -> str:
    return uuid.uuid4().hex


def run(*, env: dict[str, Any], trigger: Trigger) -> RunResult:
    """Public entry point — wraps _run_inner with metrics emission."""
    start = time.perf_counter()
    try:
        result = _run_inner(env=env, trigger=trigger)
    except Exception:
        metrics.portal_product_sync_errors_total.labels(
            env=env["slug"], reason="crash",
        ).inc()
        raise
    duration = time.perf_counter() - start
    metrics.portal_product_sync_duration_seconds.labels(
        env=env["slug"], status=result.status.value,
    ).observe(duration)
    metrics.portal_product_sync_items_total.labels(
        env=env["slug"], kind="produto", status=result.status.value,
    ).inc(result.delta_count_produtos)
    metrics.portal_product_sync_items_total.labels(
        env=env["slug"], kind="componente", status=result.status.value,
    ).inc(result.delta_count_componentes)
    metrics.portal_product_sync_items_total.labels(
        env=env["slug"], kind="tombstone", status=result.status.value,
    ).inc(result.delta_count_tombstones)
    for err in result.errors:
        metrics.portal_product_sync_errors_total.labels(
            env=env["slug"], reason=err.reason,
        ).inc()
    if result.status.value in ("applied", "partial"):
        metrics.portal_product_sync_last_success_timestamp.labels(
            env=env["slug"],
        ).set(time.time())
    return result


def _run_inner(*, env: dict[str, Any], trigger: Trigger) -> RunResult:
    """Execute one sync run for `env`. Returns RunResult (does not raise on
    sync errors — those are surfaced in `result.status` and `result.errors`)."""
    flow_cfg = environments_repo.to_flowpcp_config(env)
    sync_id = _new_sync_id()
    trace_id = sync_id  # propagate same id; could be extended with parent

    # Pre-flight
    if not flow_cfg["enabled"]:
        return RunResult(
            sync_id=sync_id, status=RunStatus.FAILED,
            errors=[SyncError(codigo="-", reason="flowpcp_disabled")],
            trace_id=trace_id,
        )
    if env.get("flowpcp_circuit_open"):
        return RunResult(
            sync_id=sync_id, status=RunStatus.FAILED,
            errors=[SyncError(codigo="-", reason="circuit_open")],
            trace_id=trace_id,
        )
    missing = [k for k in ("base_url", "tenant_id", "api_key") if not flow_cfg[k]]
    if missing:
        return RunResult(
            sync_id=sync_id, status=RunStatus.FAILED,
            errors=[SyncError(codigo="-", reason=f"flowpcp_config_missing:{','.join(missing)}")],
            trace_id=trace_id,
        )

    fb_cfg = environments_repo.to_fb_config(env)

    with active_env(env["id"], env["slug"]):
        sync_state_repo.record_run_start(
            sync_id=sync_id, trigger=trigger, trace_id=trace_id,
        )

        try:
            products = read_products_snapshot(fb_cfg)
            components = read_components_snapshot(fb_cfg)
        except Exception as exc:  # noqa: BLE001 — Fire down or schema mismatch
            logger.error(f"sync.runner: fire read failed env={env['slug']}: {exc}")
            result = RunResult(
                sync_id=sync_id, status=RunStatus.FAILED,
                errors=[SyncError(codigo="-", reason=f"fire_read_failed:{type(exc).__name__}")],
                trace_id=trace_id,
            )
            sync_state_repo.record_run_finish(sync_id=sync_id, result=result)
            environments_repo.mark_flowpcp_failure(env_id=env["id"], threshold=_FAILURE_THRESHOLD)
            return result

        product_state = sync_state_repo.load_product_state()
        component_state = sync_state_repo.load_component_state()

        delta = compute_delta(
            product_snapshot=products,
            component_snapshot=components,
            product_state=product_state,
            component_state=component_state,
        )

        if delta.is_empty():
            result = RunResult(
                sync_id=sync_id, status=RunStatus.APPLIED,
                trace_id=trace_id,
            )
            sync_state_repo.record_run_finish(sync_id=sync_id, result=result)
            environments_repo.mark_flowpcp_success(env_id=env["id"])
            return result

        # Build payloads for the wire + new state hashes (for commit on success)
        produtos_payload: list[dict[str, Any]] = []
        new_product_hashes: dict[int, str] = {}
        for item in delta.products:
            produtos_payload.append(item.payload)
            new_product_hashes[item.seq] = canonical_hash(item.payload)
        for seq in delta.tombstones:
            produtos_payload.append({"codigo": str(seq), "ativo": False})

        componentes_payload: list[dict[str, Any]] = []
        new_component_hashes: dict[int, str] = {}
        for item in delta.components:
            componentes_payload.append(item.payload)
            new_component_hashes[item.codigo] = canonical_hash(item.payload)

        client = FlowPCPClient(
            base_url=flow_cfg["base_url"],
            api_key=flow_cfg["api_key"],
            tenant_id=flow_cfg["tenant_id"],
        )

        try:
            response = client.sync_products(
                produtos=produtos_payload,
                componentes=componentes_payload,
                sync_id=sync_id,
                trace_id=trace_id,
            )
        except FlowPCPClientError as exc:
            logger.error(f"sync.runner: flowpcp send failed env={env['slug']}: {exc}")
            result = RunResult(
                sync_id=sync_id, status=RunStatus.FAILED,
                delta_count_produtos=len(delta.products),
                delta_count_componentes=len(delta.components),
                delta_count_tombstones=len(delta.tombstones),
                delta_count_component_tombstones=len(delta.component_tombstones),
                errors=[SyncError(codigo="-", reason=f"http_error:{exc.status_code or 'network'}")],
                trace_id=trace_id,
            )
            sync_state_repo.record_run_finish(sync_id=sync_id, result=result)
            environments_repo.mark_flowpcp_failure(env_id=env["id"], threshold=_FAILURE_THRESHOLD)
            return result
        finally:
            client.close()

        # Apply state, excluding any items that came back in errors.
        error_codes = {e.codigo for e in response.errors}
        product_upserts_to_commit = {
            seq: h for seq, h in new_product_hashes.items()
            if str(seq) not in error_codes
        }
        component_upserts_to_commit = {
            codigo: h for codigo, h in new_component_hashes.items()
            if str(codigo) not in error_codes
        }

        sync_state_repo.commit_states(
            product_upserts=product_upserts_to_commit,
            product_tombstones=delta.tombstones,
            component_upserts=component_upserts_to_commit,
            component_tombstones=delta.component_tombstones,
        )

        applied = (
            response.applied.get("produtos", 0)
            + response.applied.get("componentes", 0)
            + response.applied.get("tombstones", 0)
        )
        status = RunStatus.PARTIAL if response.errors else RunStatus.APPLIED

        result = RunResult(
            sync_id=sync_id, status=status,
            delta_count_produtos=len(delta.products),
            delta_count_componentes=len(delta.components),
            delta_count_tombstones=len(delta.tombstones),
            delta_count_component_tombstones=len(delta.component_tombstones),
            applied_count=applied,
            errors=[SyncError(codigo=e.codigo, reason=e.reason) for e in response.errors],
            trace_id=trace_id,
        )
        sync_state_repo.record_run_finish(sync_id=sync_id, result=result)
        environments_repo.mark_flowpcp_success(env_id=env["id"])
        return result


__all__ = ["run"]
