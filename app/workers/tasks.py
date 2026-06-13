"""Celery task wrappers around the async platform jobs.

Each task bridges the sync Celery worker to an async job via ``asyncio.run``.
Beat schedules these by name (see :mod:`app.workers.celery_app`).
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable
from typing import TypeVar

from . import jobs
from .celery_app import (
    TASK_NUDGE_RUN_DUE,
    TASK_STATUS_POLLER,
    TASK_WORKFLOW_RECOVER,
    TASK_WORKFLOW_TIMEOUT_SWEEP,
    celery_app,
)
from .status_poller import run_status_poller

_T = TypeVar("_T")


async def _run_then_dispose(coro: Awaitable[_T]) -> _T:
    """Await ``coro`` then dispose the process-singleton DB engine.

    Bug (UAT 2026-06-13): each celery tick runs in a FRESH event loop via
    ``asyncio.run``, but the ``@lru_cache`` :class:`Database` pools connections
    bound to the loop that first created them. Reusing it on the next tick's
    loop raised ``RuntimeError: ... attached to a different loop`` /
    ``Event loop is closed``, so the nudge + status jobs silently failed every
    other tick (and the docs "settle" nudge never fired). Disposing the pool at
    the end of each run — inside this loop, before ``asyncio.run`` closes it —
    lets the next tick reconnect cleanly. Best-effort; never masks the job error.
    """

    try:
        return await coro
    finally:
        try:
            from app.shared.db.provider import get_database

            await get_database().dispose()
        except Exception:  # noqa: BLE001 — cleanup must never raise
            pass


@celery_app.task(name=TASK_NUDGE_RUN_DUE)  # type: ignore[untyped-decorator]
def nudge_run_due() -> int:
    return asyncio.run(_run_then_dispose(jobs.run_due_nudges()))


@celery_app.task(name=TASK_WORKFLOW_RECOVER)  # type: ignore[untyped-decorator]
def workflow_recover() -> int:
    return asyncio.run(_run_then_dispose(jobs.recover_workflows()))


@celery_app.task(name=TASK_WORKFLOW_TIMEOUT_SWEEP)  # type: ignore[untyped-decorator]
def workflow_timeout_sweep() -> int:
    return asyncio.run(_run_then_dispose(jobs.sweep_workflow_timeouts()))


@celery_app.task(name=TASK_STATUS_POLLER)  # type: ignore[untyped-decorator]
def status_poller() -> dict[str, int]:
    """Tick the journey-status polling worker. Returns the per-bucket counts."""

    stats = asyncio.run(_run_then_dispose(run_status_poller()))
    return {
        "polled": stats.polled,
        "skipped_step": stats.skipped_step,
        "skipped_cadence": stats.skipped_cadence,
        "failed": stats.failed,
    }
