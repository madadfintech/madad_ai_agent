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


async def _dispose_db() -> None:
    """Dispose the process-singleton DB engine pool.

    Bug (UAT 2026-06-13): each celery tick runs in a FRESH event loop via
    ``asyncio.run``, but the ``@lru_cache`` :class:`Database` pools connections
    bound to the loop that first created them. Reusing it on the next tick's
    loop raised ``RuntimeError: ... attached to a different loop`` /
    ``Event loop is closed``, so the nudge + status jobs silently failed every
    other tick. Disposing the pool at the end of each run — inside this loop,
    before ``asyncio.run`` closes it — lets the next tick reconnect cleanly.
    """

    try:
        from app.shared.db.provider import get_database

        await get_database().dispose()
    except Exception:  # noqa: BLE001 — cleanup must never raise
        pass


async def _run_then_dispose(coro: Awaitable[_T]) -> _T:
    """Await ``coro`` then dispose the DB engine (for DB-only jobs)."""

    try:
        return await coro
    finally:
        await _dispose_db()


async def _run_workflow_job(coro: Awaitable[_T]) -> _T:
    """Run a job that touches workflow run state.

    On top of the per-tick DB dispose, the LangGraph Postgres checkpointer must
    be (re)bound to THIS event loop before any ``aget_state`` / resume — the
    celery worker never ran the web app's startup ``runtime.setup()``, so reading
    run state raised ``PostgresCheckpointerProvider.setup() not called`` and the
    docs settle sweep / workflow recovery never advanced any run. setup() here +
    aclose() in finally keeps each fresh-loop tick self-contained.
    """

    from app.services.workflow.deps import get_onboarding_platform

    platform = get_onboarding_platform()
    await platform.runtime.setup()
    try:
        return await coro
    finally:
        try:
            await platform.runtime.aclose()
        except Exception:  # noqa: BLE001 — cleanup must never raise
            pass
        await _dispose_db()


@celery_app.task(name=TASK_NUDGE_RUN_DUE)  # type: ignore[untyped-decorator]
def nudge_run_due() -> int:
    return asyncio.run(_run_then_dispose(jobs.run_due_nudges()))


@celery_app.task(name=TASK_WORKFLOW_RECOVER)  # type: ignore[untyped-decorator]
def workflow_recover() -> int:
    return asyncio.run(_run_workflow_job(jobs.recover_workflows()))


@celery_app.task(name=TASK_WORKFLOW_TIMEOUT_SWEEP)  # type: ignore[untyped-decorator]
def workflow_timeout_sweep() -> int:
    return asyncio.run(_run_workflow_job(jobs.sweep_workflow_timeouts()))


@celery_app.task(name=TASK_STATUS_POLLER)  # type: ignore[untyped-decorator]
def status_poller() -> dict[str, int]:
    """Tick the journey-status polling worker. Returns the per-bucket counts."""

    stats = asyncio.run(_run_workflow_job(run_status_poller()))
    return {
        "polled": stats.polled,
        "skipped_step": stats.skipped_step,
        "skipped_cadence": stats.skipped_cadence,
        "failed": stats.failed,
    }
