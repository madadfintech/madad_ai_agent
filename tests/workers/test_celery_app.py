"""Celery app construction, beat schedule, and task wrappers (sync tests).

These run as plain (non-async) tests: the task wrappers call ``asyncio.run``,
which cannot run inside an already-running event loop.
"""

from __future__ import annotations

from app.core.config import CelerySettings, Settings
from app.workers.celery_app import (
    TASK_NUDGE_RUN_DUE,
    TASK_STATUS_POLLER,
    TASK_WORKFLOW_RECOVER,
    TASK_WORKFLOW_TIMEOUT_SWEEP,
    build_celery_app,
    celery_app,
)
from app.workers.tasks import (
    nudge_run_due,
    status_poller,
    workflow_recover,
    workflow_timeout_sweep,
)


def test_beat_schedule_wires_four_jobs():
    schedule = celery_app.conf.beat_schedule
    assert {entry["task"] for entry in schedule.values()} == {
        TASK_NUDGE_RUN_DUE,
        TASK_WORKFLOW_RECOVER,
        TASK_WORKFLOW_TIMEOUT_SWEEP,
        TASK_STATUS_POLLER,
    }


def test_beat_intervals_come_from_settings():
    settings = Settings(
        celery=CelerySettings(
            nudge_run_due_seconds=5.0,
            workflow_recover_seconds=11.0,
            workflow_timeout_sweep_seconds=13.0,
            status_poller_seconds=7.0,
        )
    )
    app = build_celery_app(settings)
    by_task = {e["task"]: e["schedule"] for e in app.conf.beat_schedule.values()}
    assert by_task[TASK_NUDGE_RUN_DUE] == 5.0
    assert by_task[TASK_WORKFLOW_RECOVER] == 11.0
    assert by_task[TASK_WORKFLOW_TIMEOUT_SWEEP] == 13.0
    assert by_task[TASK_STATUS_POLLER] == 7.0


def test_broker_and_backend_from_settings():
    settings = Settings(
        celery=CelerySettings(
            broker_url="redis://example:6379/7",
            result_backend="redis://example:6379/8",
        )
    )
    app = build_celery_app(settings)
    assert app.conf.broker_url == "redis://example:6379/7"
    assert app.conf.result_backend == "redis://example:6379/8"


def test_task_names_registered():
    assert nudge_run_due.name == TASK_NUDGE_RUN_DUE
    assert workflow_recover.name == TASK_WORKFLOW_RECOVER
    assert workflow_timeout_sweep.name == TASK_WORKFLOW_TIMEOUT_SWEEP
    assert status_poller.name == TASK_STATUS_POLLER


def test_tasks_execute_against_in_memory_singletons():
    # Direct call runs the task body synchronously (asyncio.run inside).
    assert nudge_run_due() == 0
    assert workflow_recover() == 0
    assert workflow_timeout_sweep() == 0
    # Status poller returns a dict of per-bucket counts.
    stats = status_poller()
    assert set(stats.keys()) == {"polled", "skipped_step", "skipped_cadence", "failed"}
