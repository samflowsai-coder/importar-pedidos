"""APScheduler job — runs `sync.runner.run` for every env with FlowPCP enabled.

Master kill switch: `PORTAL_SYNC_ENABLED=0` skips all envs.
Scheduling cadence is set in `app/worker/scheduler.py` from
`PORTAL_SYNC_INTERVAL_MINUTES` (default 15).
"""
from __future__ import annotations

import os

from app.persistence import environments_repo
from app.sync import runner
from app.sync.models import Trigger
from app.utils.logger import logger


def _is_master_enabled() -> bool:
    return os.environ.get("PORTAL_SYNC_ENABLED", "1").strip() not in ("", "0", "false", "False")


def run_flowpcp_product_sync() -> None:
    if not _is_master_enabled():
        logger.info("flowpcp_product_sync: master switch off (PORTAL_SYNC_ENABLED=0)")
        return
    envs = environments_repo.list_active()
    candidates = [
        e for e in envs
        if e.get("flowpcp_enabled") and not e.get("flowpcp_circuit_open")
    ]
    if not candidates:
        logger.debug("flowpcp_product_sync: no enabled envs")
        return
    logger.info(f"flowpcp_product_sync: starting for {len(candidates)} env(s)")
    for env in candidates:
        try:
            result = runner.run(env=env, trigger=Trigger.SCHEDULER)
            logger.info(
                f"flowpcp_product_sync: env={env['slug']} status={result.status.value} "
                f"applied={result.applied_count} errors={len(result.errors)} "
                f"sync_id={result.sync_id}"
            )
        except Exception as exc:  # noqa: BLE001 — never let one env crash the loop
            logger.error(f"flowpcp_product_sync: env={env['slug']} crashed: {exc}")
