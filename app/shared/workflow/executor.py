"""Workflow executor — starts, resumes, and continues workflow runs.

The executor drives a workflow one *segment* at a time: each invocation runs the
graph until it either completes or pauses on an ``interrupt`` waiting for the next
inbound message. Around every segment it layers the operational concerns that
make the runtime production-grade:

* **retry** — transient failures re-drive from the last checkpoint;
* **timeout** — a segment that overruns its budget is cancelled;
* **persistence** — the run record tracks status, step, attempts, errors, timing;
* **audit + events** — every lifecycle change is recorded and published.

Determinism is preserved by binding the per-run :class:`WorkflowContext` for the
duration of each invocation, so nodes get their dependencies injected.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, cast

from langgraph.types import Command

from app.core.config import WorkflowSettings
from app.core.logging import get_logger

from .audit import AuditLogger
from .context import Clock, SystemClock, WorkflowContext, use_context
from .enums import TERMINAL_STATUSES, Channel, RunStatus, WorkflowEventType
from .errors import RetryExhaustedError, StepTimeoutError, WorkflowExecutionError
from .events import EventBus
from .loader import CompiledWorkflow, WorkflowLoader
from .persistence import WorkflowRun, WorkflowRunStore
from .retry import RetryEngine, RetryPolicy
from .session import Session, SessionManager
from .timeout import TimeoutEngine
from .transitions import TransitionManager
from .utils import derive_thread_id, new_id


@dataclass
class ExecutionResult:
    """Outcome of one start/resume/continue invocation."""

    run: WorkflowRun
    status: RunStatus
    values: dict[str, Any] = field(default_factory=dict)
    interrupts: list[dict[str, Any]] = field(default_factory=list)

    @property
    def waiting(self) -> bool:
        return self.status == RunStatus.WAITING_FOR_INPUT

    @property
    def completed(self) -> bool:
        return self.status == RunStatus.COMPLETED

    @property
    def failed(self) -> bool:
        return self.status in {RunStatus.FAILED, RunStatus.TIMED_OUT, RunStatus.DEAD_LETTERED}

    @property
    def prompt(self) -> dict[str, Any] | None:
        """The most recent interrupt payload (what to ask the user), if waiting."""

        return self.interrupts[-1] if self.interrupts else None


class WorkflowExecutor:
    """Executes workflow runs with retry, timeout, persistence, audit and events."""

    def __init__(
        self,
        *,
        loader: WorkflowLoader,
        sessions: SessionManager,
        run_store: WorkflowRunStore,
        transitions: TransitionManager,
        audit: AuditLogger,
        events: EventBus,
        retry_engine: RetryEngine,
        timeout_engine: TimeoutEngine,
        settings: WorkflowSettings,
        clock: Clock | None = None,
        logger: Any | None = None,
    ) -> None:
        self._loader = loader
        self._sessions = sessions
        self._run_store = run_store
        self._transitions = transitions
        self._audit = audit
        self._events = events
        self._retry = retry_engine
        self._timeout = timeout_engine
        self._settings = settings
        self._clock = clock or SystemClock()
        self._log = logger or get_logger("workflow.executor")

    # -- public API -----------------------------------------------------------

    async def start(
        self,
        workflow: str,
        channel: Channel,
        identity: str,
        *,
        input: Any = None,
        version: int | None = None,
        correlation_id: str | None = None,
    ) -> ExecutionResult:
        """Begin a new run of ``workflow`` for a channel-identity."""

        compiled = self._loader.load(workflow, version)
        session = await self._sessions.resolve(channel, identity)

        run_id = new_id("run")
        thread_id = derive_thread_id(
            compiled.name, compiled.version, session.session_id, run_id
        )
        run = WorkflowRun(
            run_id=run_id,
            workflow=compiled.name,
            version=compiled.version,
            session_id=session.session_id,
            thread_id=thread_id,
            channel=channel,
            identity=identity,
            correlation_id=correlation_id,
            status=RunStatus.PENDING,
        )
        await self._run_store.create(run)
        await self._sessions.bind_run(session, run)
        await self._transitions.transition(run, RunStatus.RUNNING, action="start")

        ctx = self._build_context(run)
        await ctx.emit(WorkflowEventType.RUN_STARTED, {"workflow": run.workflow})

        initial = compiled.state_schema(
            session_id=session.session_id,
            channel=channel,
            identity=identity,
            workflow=run.workflow,
            last_input=input,
            data=dict(input) if isinstance(input, dict) else {},
        )
        return await self._drive(
            compiled, run, session, ctx, first_input=initial, resume_value=None
        )

    async def resume(
        self,
        channel: Channel,
        identity: str,
        *,
        message: Any = None,
        correlation_id: str | None = None,
    ) -> ExecutionResult:
        """Resume the active run for a channel-identity with an inbound message.

        This is the reconnect path: an inbound WhatsApp/email message resolves the
        session, finds its waiting run, and feeds the message in as the resume
        value of the pending ``interrupt``.
        """

        from .errors import SessionNotFoundError

        session = await self._sessions.get(channel, identity)
        if session is None or not session.active_run_id:
            raise SessionNotFoundError(
                f"No active session for {channel.value}:{identity}"
            )
        run = await self._run_store.get(session.active_run_id)
        if run.status in TERMINAL_STATUSES:
            raise WorkflowExecutionError(
                f"Run {run.run_id} is already {run.status}",
                details={"status": str(run.status)},
            )
        if correlation_id:
            run.correlation_id = correlation_id
            await self._run_store.save(run)

        compiled = self._loader.load(run.workflow, run.version)
        await self._transitions.transition(run, RunStatus.RUNNING, action="resume")
        await self._sessions.mark_active(session)

        ctx = self._build_context(run)
        await ctx.emit(WorkflowEventType.RUN_RESUMED, {})
        return await self._drive(
            compiled,
            run,
            session,
            ctx,
            first_input=Command(resume=message),
            resume_value=message,
        )

    async def continue_run(self, run: WorkflowRun) -> ExecutionResult:
        """Re-drive a run left mid-flight by a crash (used by the recovery engine)."""

        compiled = self._loader.load(run.workflow, run.version)
        session = await self._sessions.get(
            run.channel, run.identity
        ) if run.channel else None
        if session is None and run.channel is not None:
            session = await self._sessions.resolve(run.channel, run.identity)

        if run.status != RunStatus.RUNNING:
            await self._transitions.transition(run, RunStatus.RUNNING, action="recover")

        ctx = self._build_context(run)
        await ctx.emit(WorkflowEventType.RUN_RECOVERED, {})
        return await self._drive(
            compiled, run, session, ctx, first_input=None, resume_value=None
        )

    # -- core drive loop ------------------------------------------------------

    async def _drive(
        self,
        compiled: CompiledWorkflow,
        run: WorkflowRun,
        session: Session | None,
        ctx: WorkflowContext,
        *,
        first_input: Any,
        resume_value: Any,
    ) -> ExecutionResult:
        graph = compiled.graph
        config = {"configurable": {"thread_id": run.thread_id}}
        policy = self._default_retry_policy()

        async def attempt(n: int) -> Any:
            run.attempts += 1
            if n == 0:
                command = first_input
            else:
                # On a retry, re-send the resume value only if an interrupt is
                # still pending; otherwise continue from the last checkpoint.
                snap = await graph.aget_state(config)
                if snap.interrupts and resume_value is not None:
                    command = Command(resume=resume_value)
                else:
                    command = None
            with use_context(ctx):
                return await self._timeout.run(
                    graph.ainvoke(command, config), self._settings.step_timeout_seconds
                )

        async def on_retry(attempt_no: int, exc: Exception, delay: float) -> None:
            run.last_error = f"{type(exc).__name__}: {exc}"
            await self._run_store.save(run)
            await ctx.emit(
                WorkflowEventType.RUN_RETRIED,
                {"attempt": attempt_no, "delay": delay, "error": run.last_error},
            )

        try:
            values = await self._retry.run(attempt, policy, on_retry=on_retry)
        except Exception as exc:  # noqa: BLE001 - converted to a FAILED result
            return await self._handle_failure(run, session, ctx, exc)

        return await self._finalize(compiled, run, session, ctx, config, values)

    async def _finalize(
        self,
        compiled: CompiledWorkflow,
        run: WorkflowRun,
        session: Session | None,
        ctx: WorkflowContext,
        config: dict[str, Any],
        values: Any,
    ) -> ExecutionResult:
        snap = await compiled.graph.aget_state(config)
        interrupts = self._extract_interrupts(snap)
        next_nodes = tuple(snap.next or ())

        if next_nodes:
            run.current_step = next_nodes[0]
        run.last_error = None

        if not next_nodes:
            status = RunStatus.COMPLETED
        elif interrupts:
            status = RunStatus.WAITING_FOR_INPUT
        else:
            status = RunStatus.SUSPENDED

        run.pending_interrupts = interrupts if status == RunStatus.WAITING_FOR_INPUT else []
        await self._run_store.save(run)
        await ctx.emit(WorkflowEventType.STEP_COMPLETED, {"next": list(next_nodes)})

        if status == RunStatus.COMPLETED:
            await self._transitions.transition(run, RunStatus.COMPLETED, action="complete")
            if session is not None:
                await self._sessions.complete(session)
            await ctx.emit(WorkflowEventType.RUN_COMPLETED, {})
        elif status == RunStatus.WAITING_FOR_INPUT:
            await self._transitions.transition(
                run, RunStatus.WAITING_FOR_INPUT, action="await_input"
            )
            if session is not None:
                await self._sessions.mark_waiting(session)
                run.expires_at = session.expires_at
                await self._run_store.save(run)
            await ctx.emit(WorkflowEventType.RUN_SUSPENDED, {"interrupts": interrupts})
        else:  # SUSPENDED (more work pending but engine yielded)
            await self._transitions.transition(run, RunStatus.SUSPENDED, action="suspend")
            await ctx.emit(WorkflowEventType.RUN_SUSPENDED, {})

        fresh = await self._run_store.get(run.run_id)
        return ExecutionResult(
            run=fresh,
            status=status,
            values=self._clean_values(values),
            interrupts=interrupts,
        )

    async def _handle_failure(
        self,
        run: WorkflowRun,
        session: Session | None,
        ctx: WorkflowContext,
        exc: Exception,
    ) -> ExecutionResult:
        cause = getattr(exc, "__cause__", None)
        is_timeout = isinstance(exc, StepTimeoutError) or isinstance(cause, StepTimeoutError)
        target = RunStatus.TIMED_OUT if is_timeout else RunStatus.FAILED

        run.last_error = f"{type(exc).__name__}: {exc}"
        await self._run_store.save(run)
        await self._transitions.transition(
            run, target, action="failure", detail={"error": run.last_error}
        )
        await ctx.emit(
            WorkflowEventType.RUN_TIMED_OUT if is_timeout else WorkflowEventType.RUN_FAILED,
            {"error": run.last_error, "retryable": isinstance(exc, RetryExhaustedError)},
        )
        self._log.warning(
            "workflow.run.failed",
            run_id=run.run_id,
            workflow=run.workflow,
            status=str(target),
            error=run.last_error,
        )
        fresh = await self._run_store.get(run.run_id)
        return ExecutionResult(run=fresh, status=target, values={}, interrupts=[])

    # -- helpers --------------------------------------------------------------

    def _build_context(self, run: WorkflowRun) -> WorkflowContext:
        return WorkflowContext(
            run_id=run.run_id,
            session_id=run.session_id,
            thread_id=run.thread_id,
            workflow=run.workflow,
            version=run.version,
            channel=run.channel,
            identity=run.identity,
            clock=self._clock,
            events=self._events,
            logger=self._log,
            correlation_id=run.correlation_id,
        )

    def _default_retry_policy(self) -> RetryPolicy:
        return RetryPolicy(
            max_attempts=self._settings.retry_max_attempts,
            base_delay=self._settings.retry_base_delay_seconds,
            max_delay=self._settings.retry_max_delay_seconds,
            jitter=self._settings.retry_jitter,
        )

    @staticmethod
    def _extract_interrupts(snap: Any) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        for it in getattr(snap, "interrupts", ()) or ():
            val = getattr(it, "value", it)
            out.append(val if isinstance(val, dict) else {"value": val})
        return out

    @staticmethod
    def _clean_values(values: Any) -> dict[str, Any]:
        if isinstance(values, dict):
            return {k: v for k, v in values.items() if k != "__interrupt__"}
        if hasattr(values, "model_dump"):
            return cast(dict[str, Any], values.model_dump())
        return {"value": values}
