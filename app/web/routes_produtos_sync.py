"""Admin routes for product sync (Portal → FlowPCP).

GET  /admin/produtos/sync/{slug}               — last 50 runs + env config snapshot
POST /admin/produtos/sync-now/{slug}           — fire one sync inline (manual trigger)
POST /admin/produtos/sync/{slug}/reset-circuit — clear circuit-breaker flag

All require admin auth.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException

from app.persistence import environments_repo
from app.persistence.context import active_env
from app.sync import runner, sync_state_repo
from app.sync.models import Trigger
from app.web.auth import require_admin

router = APIRouter(tags=["admin", "produtos-sync"])


def _env_or_404(slug: str) -> dict:
    env = environments_repo.get_by_slug(slug)
    if not env:
        raise HTTPException(status_code=404, detail="environment not found")
    return env


@router.get("/admin/produtos/sync/{slug}")
def get_runs(slug: str, _admin=Depends(require_admin)):
    env = _env_or_404(slug)
    with active_env(env["id"], env["slug"]):
        runs = sync_state_repo.list_runs(limit=50)
    return {
        "env": {
            "slug": env["slug"],
            "name": env["name"],
            "flowpcp_enabled": bool(env.get("flowpcp_enabled")),
            "flowpcp_base_url": env.get("flowpcp_base_url"),
            "flowpcp_tenant_id": env.get("flowpcp_tenant_id"),
            "flowpcp_circuit_open": bool(env.get("flowpcp_circuit_open")),
            "flowpcp_consecutive_failures": int(env.get("flowpcp_consecutive_failures") or 0),
            "flowpcp_last_failure_at": env.get("flowpcp_last_failure_at"),
        },
        "runs": runs,
    }


@router.post("/admin/produtos/sync-now/{slug}")
def sync_now(slug: str, _admin=Depends(require_admin)):
    env = _env_or_404(slug)
    result = runner.run(env=env, trigger=Trigger.MANUAL)
    return {
        "sync_id": result.sync_id,
        "status": result.status.value,
        "delta_count_produtos": result.delta_count_produtos,
        "delta_count_componentes": result.delta_count_componentes,
        "delta_count_tombstones": result.delta_count_tombstones,
        "delta_count_component_tombstones": result.delta_count_component_tombstones,
        "applied_count": result.applied_count,
        "errors": [e.model_dump() for e in result.errors],
    }


@router.post("/admin/produtos/sync/{slug}/reset-circuit")
def reset_circuit(slug: str, _admin=Depends(require_admin)):
    env = _env_or_404(slug)
    environments_repo.reset_flowpcp_circuit(env["id"])
    return {"ok": True}
