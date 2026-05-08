"""Bootstrap APScheduler with SQLAlchemyJobStore on the app's SQLite.

Run with:  python -m app.worker

Recurring jobs:
  drain_outbox            — every 15s, drains pending rows from the outbox table.
  poll_fire               — every 60s, polls Firebird for order status changes.
  flowpcp_product_sync    — every N min (default 15), syncs products to FlowPCP.

All jobs use coalesce=True + max_instances=1 to prevent pile-up when a run
takes longer than the interval.
"""
from __future__ import annotations

import os
import signal
from typing import Any

from apscheduler.executors.pool import ThreadPoolExecutor
from apscheduler.jobstores.sqlalchemy import SQLAlchemyJobStore
from apscheduler.schedulers.blocking import BlockingScheduler

from app.persistence.db import db_path
from app.persistence.db import init as db_init
from app.utils.logger import logger
from app.worker.jobs.drain_outbox import run_drain_outbox
from app.worker.jobs.flowpcp_product_sync import run_flowpcp_product_sync
from app.worker.jobs.poll_fire import run_poll_fire
from app.worker.jobs.retention import run_retention
from app.worker.jobs.scan_environments import run_scan as run_scan_environments

_DRAIN_INTERVAL_S = 15
_POLL_INTERVAL_S = 60
_SCAN_INTERVAL_S = 30  # watcher multi-pasta — ingesta arquivos novos por env
_RETENTION_HOUR = 3  # 03:00 local — low-traffic window
_FLOWPCP_SYNC_INTERVAL_M = int(os.environ.get("PORTAL_SYNC_INTERVAL_MINUTES", "15"))


def start() -> None:
    """Initialize schema, configure scheduler, block until signal."""
    db_init()

    jobstore_url = f"sqlite:///{db_path()}"
    scheduler = BlockingScheduler(
        jobstores={"default": SQLAlchemyJobStore(url=jobstore_url)},
        executors={"default": ThreadPoolExecutor(max_workers=2)},
        job_defaults={
            "coalesce": True,
            "max_instances": 1,
            "misfire_grace_time": 30,
        },
    )

    scheduler.add_job(
        run_drain_outbox,
        "interval",
        seconds=_DRAIN_INTERVAL_S,
        id="drain_outbox",
        replace_existing=True,
    )
    scheduler.add_job(
        run_poll_fire,
        "interval",
        seconds=_POLL_INTERVAL_S,
        id="poll_fire",
        replace_existing=True,
    )
    scheduler.add_job(
        run_retention,
        "cron",
        hour=_RETENTION_HOUR,
        id="retention",
        replace_existing=True,
    )
    scheduler.add_job(
        run_scan_environments,
        "interval",
        seconds=_SCAN_INTERVAL_S,
        id="scan_environments",
        replace_existing=True,
    )
    scheduler.add_job(
        run_flowpcp_product_sync,
        "interval",
        minutes=_FLOWPCP_SYNC_INTERVAL_M,
        id="flowpcp_product_sync",
        replace_existing=True,
    )

    def _shutdown(sig: Any, _frame: Any) -> None:
        logger.info("worker.shutdown signal={}", sig)
        scheduler.shutdown(wait=False)

    signal.signal(signal.SIGTERM, _shutdown)
    signal.signal(signal.SIGINT, _shutdown)

    logger.info(
        "worker.start drain={}s poll={}s retention_hour={} flowpcp_sync={}m",
        _DRAIN_INTERVAL_S,
        _POLL_INTERVAL_S,
        _RETENTION_HOUR,
        _FLOWPCP_SYNC_INTERVAL_M,
    )
    scheduler.start()
