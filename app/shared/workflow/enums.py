"""Enumerations for the workflow runtime."""

from __future__ import annotations

from enum import StrEnum


class Channel(StrEnum):
    """A conversational channel. The channel + identity together are the
    session key (channel-is-identity principle — no OTP)."""

    WHATSAPP = "whatsapp"
    EMAIL = "email"


class RunStatus(StrEnum):
    """Lifecycle status of a single workflow run (a workflow instance bound to
    one session)."""

    PENDING = "pending"
    RUNNING = "running"
    WAITING_FOR_INPUT = "waiting_for_input"
    SUSPENDED = "suspended"  # paused mid-flight (e.g. crash) — recoverable
    COMPLETED = "completed"
    FAILED = "failed"
    TIMED_OUT = "timed_out"
    DEAD_LETTERED = "dead_lettered"
    CANCELLED = "cancelled"


TERMINAL_STATUSES: frozenset[RunStatus] = frozenset(
    {
        RunStatus.COMPLETED,
        RunStatus.FAILED,
        RunStatus.DEAD_LETTERED,
        RunStatus.CANCELLED,
    }
)

# Statuses a crash-recovery sweep should attempt to re-drive.
RECOVERABLE_STATUSES: frozenset[RunStatus] = frozenset(
    {RunStatus.RUNNING, RunStatus.SUSPENDED}
)


class SessionStatus(StrEnum):
    """Lifecycle status of a channel-identity session."""

    ACTIVE = "active"
    WAITING = "waiting"  # awaiting inbound message from the user
    COMPLETED = "completed"
    EXPIRED = "expired"
    CLOSED = "closed"


class WorkflowEventType(StrEnum):
    """Event types emitted by the runtime onto the event bus."""

    RUN_STARTED = "workflow.run.started"
    STEP_COMPLETED = "workflow.step.completed"
    RUN_SUSPENDED = "workflow.run.suspended"  # waiting for input
    RUN_RESUMED = "workflow.run.resumed"
    RUN_COMPLETED = "workflow.run.completed"
    RUN_FAILED = "workflow.run.failed"
    RUN_RETRIED = "workflow.run.retried"
    RUN_TIMED_OUT = "workflow.run.timed_out"
    RUN_RECOVERED = "workflow.run.recovered"
    RUN_DEAD_LETTERED = "workflow.run.dead_lettered"
    SESSION_EXPIRED = "workflow.session.expired"
