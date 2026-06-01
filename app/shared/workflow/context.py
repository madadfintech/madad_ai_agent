"""Per-execution workflow context — the dependency carrier injected into nodes.

Nodes never reach for globals or construct their own clients. Everything a node
is allowed to touch (clock, logger, event emission, and business ports injected
by services) arrives via :class:`WorkflowContext`. This is what keeps workflows
deterministic and unit-testable.

The active context is propagated to nodes through a :class:`~contextvars.ContextVar`
set by the executor around each graph invocation.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from .enums import Channel, WorkflowEventType
from .events import EventBus, WorkflowEvent
from .utils import utcnow


class Clock(ABC):
    """Time source. Injectable so tests can freeze/advance time deterministically."""

    @abstractmethod
    def now(self) -> datetime: ...


class SystemClock(Clock):
    def now(self) -> datetime:
        return utcnow()


@dataclass
class WorkflowContext:
    """Everything a node may use during a single execution segment."""

    run_id: str
    session_id: str
    thread_id: str
    workflow: str
    version: int
    channel: Channel | None
    identity: str
    clock: Clock
    events: EventBus
    logger: Any
    correlation_id: str | None = None
    # Business ports (CMS reader, Madad API client, MCP client, ...) injected by
    # the owning service. The runtime itself leaves this empty.
    deps: dict[str, Any] = field(default_factory=dict)

    @property
    def log(self) -> Any:
        return self.logger

    async def emit(
        self, event_type: WorkflowEventType, payload: dict[str, Any] | None = None
    ) -> None:
        """Publish a workflow event stamped with this context's identity."""

        await self.events.publish(
            WorkflowEvent(
                type=event_type,
                run_id=self.run_id,
                session_id=self.session_id,
                workflow=self.workflow,
                channel=self.channel,
                identity=self.identity,
                correlation_id=self.correlation_id,
                payload=payload or {},
            )
        )


_CURRENT: ContextVar[WorkflowContext | None] = ContextVar(
    "madad_workflow_context", default=None
)


def current_context() -> WorkflowContext:
    """Return the context for the running execution, or raise if none is active.

    Called by node wrappers; a missing context means a node ran outside the
    executor, which is a programming error.
    """

    ctx = _CURRENT.get()
    if ctx is None:  # pragma: no cover - defensive
        raise RuntimeError("No active WorkflowContext; node ran outside the executor.")
    return ctx


@contextmanager
def use_context(ctx: WorkflowContext) -> Iterator[WorkflowContext]:
    """Bind ``ctx`` as the active context for the duration of a graph invocation."""

    token = _CURRENT.set(ctx)
    try:
        yield ctx
    finally:
        _CURRENT.reset(token)
