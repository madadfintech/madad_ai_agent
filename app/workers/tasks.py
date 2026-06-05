"""Celery task wrappers around the async platform jobs.

Each task bridges the sync Celery worker to an async job via ``asyncio.run``.
Beat schedules these by name (see :mod:`app.workers.celery_app`).
"""

from __future__ import annotations

import asyncio

from . import jobs
from .celery_app import (
    TASK_NUDGE_RUN_DUE,
    TASK_STATUS_POLLER,
    TASK_WORKFLOW_RECOVER,
    TASK_WORKFLOW_TIMEOUT_SWEEP,
    celery_app,
)
from .status_poller import run_status_poller


@celery_app.task(name=TASK_NUDGE_RUN_DUE)  # type: ignore[untyped-decorator]
def nudge_run_due() -> int:
    return asyncio.run(jobs.run_due_nudges())


@celery_app.task(name=TASK_WORKFLOW_RECOVER)  # type: ignore[untyped-decorator]
def workflow_recover() -> int:
    return asyncio.run(jobs.recover_workflows())


@celery_app.task(name=TASK_WORKFLOW_TIMEOUT_SWEEP)  # type: ignore[untyped-decorator]
def workflow_timeout_sweep() -> int:
    return asyncio.run(jobs.sweep_workflow_timeouts())


@celery_app.task(name=TASK_STATUS_POLLER)  # type: ignore[untyped-decorator]
def status_poller() -> dict[str, int]:
    """Tick the journey-status polling worker. Returns the per-bucket counts."""

    stats = asyncio.run(run_status_poller())
    return {
        "polled": stats.polled,
        "skipped_step": stats.skipped_step,
        "skipped_cadence": stats.skipped_cadence,
        "failed": stats.failed,
    }
