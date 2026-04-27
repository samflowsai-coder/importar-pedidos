"""Bootstrap APScheduler with SQLAlchemyJobStore on the app's SQLite.

Run with:  python -m app.worker

Two recurring jobs:
  drain_outbox — every 15s, drains pending rows from the outbox table.
  poll_fire    — every 60s, polls Firebird for order status changes.

Both use coalesce=True + max_instances=1 to prevent pile-up when a run
takes longer than the interval.
"""
from __future__ import annotations

import signal
from typing import Any

from apscheduler.executors.pool import ThreadPoolExecutor
from apscheduler.jobstores.sqlalchemy import SQLAlchemyJobStore
from apscheduler.schedulers.blocking import BlockingScheduler

from app.persistence.db import db_path
from app.persistence.db import init as db_init
from app.utils.logger import logger
from app.worker.jobs.drain_outbox import run_drain_outbox
from app.worker.jobs.poll_fire import run_poll_fire

_DRAIN_INTERVAL_S = 15
_POLL_INTERVAL_S = 60


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

    def _shutdown(sig: Any, _frame: Any) -> None:
        logger.info("worker.shutdown signal={}", sig)
        scheduler.shutdown(wait=False)

    signal.signal(signal.SIGTERM, _shutdown)
    signal.signal(signal.SIGINT, _shutdown)

    logger.info(
        "worker.start drain_interval={}s poll_interval={}s",
        _DRAIN_INTERVAL_S,
        _POLL_INTERVAL_S,
    )
    scheduler.start()
