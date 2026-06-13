"""Celery task wrappers around the async platform jobs.

Each task runs on a PERSISTENT per-process event loop (not ``asyncio.run`` per
tick). The async SQLAlchemy engine and the LangGraph Postgres checkpointer are
both event-loop-bound: with a fresh loop per tick they raised
``RuntimeError: ... attached to a different loop`` / ``the connection is
closed`` / ``PostgresCheckpointerProvider.setup() not called``, which silently
broke the nudge + status-poller jobs (UAT 2026-06-13). Keeping one loop per
worker process — and running ``runtime.setup()`` once on it — lets those pooled
connections be reused across ticks. (Celery prefork: each fork is its own
process, so each keeps its own loop + setup.)

Beat schedules these by name (see :mod:`app.workers.celery_app`).
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable
from typing import Any, TypeVar

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

# Persistent event loop for THIS worker process + whether the workflow runtime
# (Postgres checkpointer) has been set up on it yet.
_loop: asyncio.AbstractEventLoop | None = None
_workflow_setup_done = False


def _get_loop() -> asyncio.AbstractEventLoop:
    global _loop, _workflow_setup_done
    if _loop is None or _loop.is_closed():
        _loop = asyncio.new_event_loop()
        asyncio.set_event_loop(_loop)
        _workflow_setup_done = False  # a new loop needs the checkpointer set up
    return _loop


async def _ensure_workflow_setup() -> None:
    """Provision the LangGraph Postgres checkpointer once per loop (the celery
    worker never runs the web app's startup ``runtime.setup()``)."""

    global _workflow_setup_done
    if _workflow_setup_done:
        return
    from app.services.workflow.deps import get_onboarding_platform

    await get_onboarding_platform().runtime.setup()
    _workflow_setup_done = True


def _run(coro: Awaitable[_T]) -> _T:
    return _get_loop().run_until_complete(coro)


@celery_app.task(name=TASK_NUDGE_RUN_DUE)  # type: ignore[untyped-decorator]
def nudge_run_due() -> int:
    return _run(jobs.run_due_nudges())


@celery_app.task(name=TASK_WORKFLOW_RECOVER)  # type: ignore[untyped-decorator]
def workflow_recover() -> int:
    async def _go() -> int:
        await _ensure_workflow_setup()
        return await jobs.recover_workflows()

    return _run(_go())


@celery_app.task(name=TASK_WORKFLOW_TIMEOUT_SWEEP)  # type: ignore[untyped-decorator]
def workflow_timeout_sweep() -> int:
    async def _go() -> int:
        await _ensure_workflow_setup()
        return await jobs.sweep_workflow_timeouts()

    return _run(_go())


@celery_app.task(name=TASK_STATUS_POLLER)  # type: ignore[untyped-decorator]
def status_poller() -> dict[str, int]:
    """Tick the journey-status polling worker. Returns the per-bucket counts."""

    async def _go() -> Any:
        await _ensure_workflow_setup()
        return await run_status_poller()

    stats = _run(_go())
    return {
        "polled": stats.polled,
        "skipped_step": stats.skipped_step,
        "skipped_cadence": stats.skipped_cadence,
        "failed": stats.failed,
    }
