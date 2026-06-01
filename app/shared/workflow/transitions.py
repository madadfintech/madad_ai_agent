"""Workflow transition manager — guarded run-status transitions.

All run-status changes flow through here so that (a) illegal transitions are
rejected and (b) every transition is audited. This is the single place that
defines the run lifecycle state machine.
"""

from __future__ import annotations

from typing import Any

from .audit import AuditLogger
from .enums import TERMINAL_STATUSES, RunStatus
from .errors import InvalidTransitionError
from .persistence import WorkflowRun, WorkflowRunStore
from .utils import utcnow

# Allowed run-status transitions. Same-state transitions are treated as no-ops.
_ALLOWED: dict[RunStatus, frozenset[RunStatus]] = {
    RunStatus.PENDING: frozenset({RunStatus.RUNNING, RunStatus.CANCELLED}),
    RunStatus.RUNNING: frozenset(
        {
            RunStatus.WAITING_FOR_INPUT,
            RunStatus.SUSPENDED,
            RunStatus.COMPLETED,
            RunStatus.FAILED,
            RunStatus.TIMED_OUT,
            RunStatus.CANCELLED,
        }
    ),
    RunStatus.WAITING_FOR_INPUT: frozenset(
        {RunStatus.RUNNING, RunStatus.TIMED_OUT, RunStatus.CANCELLED}
    ),
    RunStatus.SUSPENDED: frozenset(
        {RunStatus.RUNNING, RunStatus.FAILED, RunStatus.DEAD_LETTERED, RunStatus.CANCELLED}
    ),
    RunStatus.TIMED_OUT: frozenset(
        {RunStatus.RUNNING, RunStatus.DEAD_LETTERED, RunStatus.CANCELLED}
    ),
    RunStatus.FAILED: frozenset(
        {RunStatus.RUNNING, RunStatus.DEAD_LETTERED, RunStatus.CANCELLED}
    ),
    RunStatus.DEAD_LETTERED: frozenset({RunStatus.RUNNING}),
    RunStatus.COMPLETED: frozenset(),
    RunStatus.CANCELLED: frozenset(),
}


class TransitionManager:
    """Validates and applies run-status transitions, persisting and auditing them."""

    def __init__(self, run_store: WorkflowRunStore, audit: AuditLogger) -> None:
        self._store = run_store
        self._audit = audit

    @staticmethod
    def is_allowed(src: RunStatus, dst: RunStatus) -> bool:
        if src == dst:
            return True
        return dst in _ALLOWED.get(src, frozenset())

    async def transition(
        self,
        run: WorkflowRun,
        to: RunStatus,
        *,
        action: str | None = None,
        detail: dict[str, Any] | None = None,
    ) -> WorkflowRun:
        """Move ``run`` to status ``to``, persisting and auditing the change.

        Raises :class:`InvalidTransitionError` for an illegal transition.
        """

        src = run.status
        if not self.is_allowed(src, to):
            raise InvalidTransitionError(
                f"Illegal transition {src} -> {to} for run {run.run_id}",
                details={"from": str(src), "to": str(to)},
            )

        run.status = to
        run.updated_at = utcnow()
        if to in TERMINAL_STATUSES and run.completed_at is None:
            run.completed_at = run.updated_at

        await self._store.save(run)
        await self._audit.record(
            run.run_id,
            action or f"transition:{src}->{to}",
            from_status=src,
            to_status=to,
            detail=detail,
        )
        return run
