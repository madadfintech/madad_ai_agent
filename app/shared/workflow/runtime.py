"""Workflow runtime facade — the single entry point services use.

``build_runtime`` wires the whole runtime together, selecting in-memory or
production adapters based on settings. A service registers its workflow
definitions, then calls :meth:`WorkflowRuntime.start` / :meth:`resume`; the
recovery engine is driven separately by a periodic job.

Adapter selection:
* checkpointer: ``memory`` (InMemorySaver) | ``postgres`` (AsyncPostgresSaver)
* event bus:    ``memory`` (in-process) | ``redis`` (Redis Streams)
* session store:``memory``             | ``redis``

The run store is currently always in-memory; the Postgres run store lands with
the platform DB foundation (the executor is already decoupled from it via the
:class:`WorkflowRunStore` port).
"""

from __future__ import annotations

from typing import Any

from app.core.config import Settings
from app.core.config import settings as default_settings

from .audit import AuditLogger
from .checkpoint import CheckpointerProvider, InMemoryCheckpointerProvider
from .context import Clock
from .definition import WorkflowDefinition
from .enums import Channel
from .events import EventBus, InMemoryEventBus
from .executor import ExecutionResult, WorkflowExecutor
from .loader import WorkflowLoader
from .persistence import InMemoryWorkflowRunStore, WorkflowRunStore
from .recovery import RecoveryEngine
from .registry import WorkflowRegistry
from .retry import RetryEngine, SleepFn
from .session import InMemorySessionStore, SessionManager, SessionStore
from .timeout import TimeoutEngine
from .transitions import TransitionManager


class WorkflowRuntime:
    """Fully-wired workflow runtime."""

    def __init__(
        self,
        *,
        registry: WorkflowRegistry,
        loader: WorkflowLoader,
        executor: WorkflowExecutor,
        sessions: SessionManager,
        run_store: WorkflowRunStore,
        events: EventBus,
        recovery: RecoveryEngine,
        checkpointer: CheckpointerProvider,
    ) -> None:
        self.registry = registry
        self.loader = loader
        self.executor = executor
        self.sessions = sessions
        self.run_store = run_store
        self.events = events
        self.recovery = recovery
        self.checkpointer = checkpointer

    # -- lifecycle ------------------------------------------------------------

    async def setup(self) -> None:
        await self.checkpointer.setup()

    async def aclose(self) -> None:
        await self.checkpointer.aclose()

    # -- registration ---------------------------------------------------------

    def register(self, definition: WorkflowDefinition) -> WorkflowDefinition:
        registered = self.registry.register(definition)
        self.loader.invalidate(definition.name, definition.version)
        return registered

    # -- convenience pass-throughs -------------------------------------------

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
        return await self.executor.start(
            workflow,
            channel,
            identity,
            input=input,
            version=version,
            correlation_id=correlation_id,
        )

    async def resume(
        self,
        channel: Channel,
        identity: str,
        *,
        message: Any = None,
        correlation_id: str | None = None,
        io_channel: Channel | None = None,
        io_identity: str | None = None,
    ) -> ExecutionResult:
        return await self.executor.resume(
            channel,
            identity,
            message=message,
            correlation_id=correlation_id,
            io_channel=io_channel,
            io_identity=io_identity,
        )

    async def revive_failed_run(self, run: Any) -> None:
        """Transition a terminally-failed run back to RUNNING so the next
        ``resume()`` can replay an inbound at the last checkpoint
        (QA #2, 2026-06-09). The audit transition records the revive
        action so the failure → recovery sequence is traceable."""

        from .enums import RunStatus
        await self.executor._transitions.transition(  # noqa: SLF001 — intentional cross-package access
            run, RunStatus.RUNNING, action="revive"
        )

    async def recover(self, limit: int | None = None) -> list[ExecutionResult]:
        return await self.recovery.recover_pending(limit)


def build_runtime(
    settings: Settings | None = None,
    *,
    registry: WorkflowRegistry | None = None,
    clock: Clock | None = None,
    sleep: SleepFn | None = None,
) -> WorkflowRuntime:
    """Construct a :class:`WorkflowRuntime` with adapters chosen from settings."""

    settings = settings or default_settings
    wf = settings.workflow
    registry = registry or WorkflowRegistry()

    checkpointer = _build_checkpointer(settings)
    events = _build_event_bus(settings)
    session_store = _build_session_store(settings)
    run_store: WorkflowRunStore = _build_run_store(settings)

    loader = WorkflowLoader(registry, checkpointer)
    audit = AuditLogger(run_store)
    transitions = TransitionManager(run_store, audit)
    sessions = SessionManager(
        session_store, clock=clock, ttl_seconds=wf.session_ttl_seconds
    )
    retry_engine = RetryEngine(sleep=sleep)
    timeout_engine = TimeoutEngine()

    executor = WorkflowExecutor(
        loader=loader,
        sessions=sessions,
        run_store=run_store,
        transitions=transitions,
        audit=audit,
        events=events,
        retry_engine=retry_engine,
        timeout_engine=timeout_engine,
        settings=wf,
        clock=clock,
    )
    recovery = RecoveryEngine(
        run_store=run_store,
        executor=executor,
        sessions=sessions,
        transitions=transitions,
        timeout_engine=timeout_engine,
        events=events,
        settings=wf,
        clock=clock,
    )

    return WorkflowRuntime(
        registry=registry,
        loader=loader,
        executor=executor,
        sessions=sessions,
        run_store=run_store,
        events=events,
        recovery=recovery,
        checkpointer=checkpointer,
    )


def _build_checkpointer(settings: Settings) -> CheckpointerProvider:
    backend = settings.workflow.checkpoint_backend
    if backend == "memory":
        return InMemoryCheckpointerProvider()
    if backend == "postgres":
        from .adapters.postgres import PostgresCheckpointerProvider

        return PostgresCheckpointerProvider(settings.postgres)
    raise ValueError(f"Unknown checkpoint_backend: {backend!r}")


def _build_event_bus(settings: Settings) -> EventBus:
    backend = settings.workflow.event_backend
    if backend == "memory":
        return InMemoryEventBus()
    if backend == "redis":
        from .adapters.redis import RedisStreamEventBus

        return RedisStreamEventBus(settings.redis)
    raise ValueError(f"Unknown event_backend: {backend!r}")


def _build_session_store(settings: Settings) -> SessionStore:
    backend = settings.workflow.session_backend
    if backend == "memory":
        return InMemorySessionStore()
    if backend == "redis":
        from .adapters.redis import RedisSessionStore

        return RedisSessionStore(settings.redis)
    raise ValueError(f"Unknown session_backend: {backend!r}")


def _build_run_store(settings: Settings) -> WorkflowRunStore:
    if settings.persistence.backend == "postgres":
        from app.shared.db.provider import get_database

        from .adapters.postgres_runstore import PostgresWorkflowRunStore

        return PostgresWorkflowRunStore(get_database())
    return InMemoryWorkflowRunStore()
