"""Celery application + beat schedule for periodic platform jobs.

Run a worker:  ``celery -A app.workers.celery_app worker -Q madad``
Run beat:      ``celery -A app.workers.celery_app beat``

Tasks live in :mod:`app.workers.tasks` and are imported lazily by Celery via the
``include`` list, which avoids an import cycle (tasks import this module).
"""

from __future__ import annotations

from celery import Celery

from app.core.config import Settings
from app.core.config import settings as default_settings

TASK_NUDGE_RUN_DUE = "madad.nudge.run_due"
TASK_WORKFLOW_RECOVER = "madad.workflow.recover"
TASK_WORKFLOW_TIMEOUT_SWEEP = "madad.workflow.timeout_sweep"
TASK_STATUS_POLLER = "madad.workflow.status_poller"


def build_celery_app(settings: Settings | None = None) -> Celery:
    """Construct the Celery app with broker, result backend and beat schedule."""

    settings = settings or default_settings
    celery = settings.celery

    app = Celery(
        "madad",
        broker=celery.broker_url,
        backend=celery.result_backend,
        include=["app.workers.tasks"],
    )
    app.conf.update(
        timezone=celery.timezone,
        enable_utc=True,
        task_serializer="json",
        result_serializer="json",
        accept_content=["json"],
        task_default_queue="madad",
        task_acks_late=True,
        worker_prefetch_multiplier=1,
        beat_schedule={
            "nudge-run-due": {
                "task": TASK_NUDGE_RUN_DUE,
                "schedule": celery.nudge_run_due_seconds,
            },
            "workflow-recover": {
                "task": TASK_WORKFLOW_RECOVER,
                "schedule": celery.workflow_recover_seconds,
            },
            "workflow-timeout-sweep": {
                "task": TASK_WORKFLOW_TIMEOUT_SWEEP,
                "schedule": celery.workflow_timeout_sweep_seconds,
            },
            "workflow-status-poller": {
                "task": TASK_STATUS_POLLER,
                "schedule": celery.status_poller_seconds,
            },
        },
    )
    return app


celery_app = build_celery_app()
