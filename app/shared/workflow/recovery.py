"""Recovery engine — crash recovery and lapsed-session sweeps.

Two responsibilities:

* **Crash recovery** — on restart, re-drive runs left in a recoverable state
  (``running``/``suspended``) from their last checkpoint. Reconnect recovery (a
  user replying after a pause) is handled directly by ``executor.resume``.
* **Timeout sweep** — find runs whose waiting session has lapsed, mark them timed
  out, and emit events so the Nudge service can act.

Both are designed to be driven periodically (e.g. a Celery beat job) and to be
resilient: one failing run never aborts the whole sweep.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from app.core.config import WorkflowSettings
from app.core.logging import get_logger

from .context import Clock, SystemClock
from .enums import RunStatus, WorkflowEventType
from .events import EventBus, WorkflowEvent
from .executor import ExecutionResult, WorkflowExecutor
from .persistence import WorkflowRun, WorkflowRunStore
from .session import SessionManager
from .timeout import TimeoutEngine
from .transitions import TransitionManager


class RecoveryEngine:
    """Re-drives interrupted runs and sweeps lapsed waiting sessions."""

    def __init__(
        self,
        *,
        run_store: WorkflowRunStore,
        executor: WorkflowExecutor,
        sessions: SessionManager,
        transitions: TransitionManager,
        timeout_engine: TimeoutEngine,
        events: EventBus,
        settings: WorkflowSettings,
        clock: Clock | None = None,
        logger: Any | None = None,
    ) -> None:
        self._run_store = run_store
        self._executor = executor
        self._sessions = sessions
        self._transitions = transitions
        self._timeout = timeout_engine
        self._events = events
        self._settings = settings
        self._clock = clock or SystemClock()
        self._log = logger or get_logger("workflow.recovery")

    async def recover_pending(self, limit: int | None = None) -> list[ExecutionResult]:
        """Re-drive all runs left mid-flight by a crash."""

        batch = limit or self._settings.recovery_batch_size
        runs = await self._run_store.list_recoverable(batch)
        results: list[ExecutionResult] = []
        for run in runs:
            try:
                results.append(await self._executor.continue_run(run))
            except Exception:  # noqa: BLE001 - never abort the sweep
                self._log.exception("workflow.recovery.failed", run_id=run.run_id)
        if runs:
            self._log.info("workflow.recovery.swept", count=len(runs))
        return results

    async def recover_run(self, run_id: str) -> ExecutionResult:
        run = await self._run_store.get(run_id)
        return await self._executor.continue_run(run)

    async def sweep_timeouts(self, now: datetime | None = None) -> list[WorkflowRun]:
        """Time out runs whose waiting session has lapsed; emit expiry events."""

        now = now or self._clock.now()
        expired = await self._timeout.find_expired(
            self._run_store, now, self._settings.recovery_batch_size
        )
        for run in expired:
            try:
                await self._transitions.transition(
                    run, RunStatus.TIMED_OUT, action="session_timeout"
                )
                await self._emit(run, WorkflowEventType.RUN_TIMED_OUT, {"reason": "session_lapsed"})
                if run.channel is not None:
                    session = await self._sessions.get(run.channel, run.identity)
                    if session is not None:
                        await self._sessions.expire(session)
                await self._emit(run, WorkflowEventType.SESSION_EXPIRED, {})
            except Exception:  # noqa: BLE001 - never abort the sweep
                self._log.exception("workflow.timeout.failed", run_id=run.run_id)
        if expired:
            self._log.info("workflow.timeout.swept", count=len(expired))
        return expired

    async def _emit(
        self, run: WorkflowRun, event_type: WorkflowEventType, payload: dict[str, Any]
    ) -> None:
        await self._events.publish(
            WorkflowEvent(
                type=event_type,
                run_id=run.run_id,
                session_id=run.session_id,
                workflow=run.workflow,
                channel=run.channel,
                identity=run.identity,
                correlation_id=run.correlation_id,
                payload=payload,
            )
        )
