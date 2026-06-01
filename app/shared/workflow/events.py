"""Workflow event framework.

A small, typed event envelope plus an :class:`EventBus` port. The runtime emits
lifecycle events (started, suspended, completed, failed, recovered, ...) so other
services (Nudge, Operational Visibility) can react without coupling to the
runtime internals.

The in-memory bus is the default (zero external dependencies, used in tests and
dev). The Redis Streams adapter in ``adapters/redis.py`` is the production bus.
"""

from __future__ import annotations

import asyncio
from abc import ABC, abstractmethod
from collections.abc import Awaitable, Callable
from typing import Any

from pydantic import BaseModel, Field

from .enums import Channel, WorkflowEventType
from .utils import new_id, utcnow

EventHandler = Callable[["WorkflowEvent"], Awaitable[None]]


class WorkflowEvent(BaseModel):
    """An immutable record of something that happened to a workflow run."""

    event_id: str = Field(default_factory=lambda: new_id("evt"))
    type: WorkflowEventType
    occurred_at: str = Field(default_factory=lambda: utcnow().isoformat())
    run_id: str
    session_id: str
    workflow: str
    channel: Channel | None = None
    identity: str = ""
    correlation_id: str | None = None
    payload: dict[str, Any] = Field(default_factory=dict)


class EventBus(ABC):
    """Port for publishing and (optionally) subscribing to workflow events."""

    @abstractmethod
    async def publish(self, event: WorkflowEvent) -> None: ...

    @abstractmethod
    def subscribe(self, handler: EventHandler) -> None:
        """Register an in-process handler. Production buses may no-op this in
        favour of external consumer groups."""


class InMemoryEventBus(EventBus):
    """Default bus: keeps a history and fans out to in-process handlers.

    Handler failures are isolated so one bad subscriber cannot break a run.
    """

    def __init__(self) -> None:
        self._handlers: list[EventHandler] = []
        self.history: list[WorkflowEvent] = []

    def subscribe(self, handler: EventHandler) -> None:
        self._handlers.append(handler)

    async def publish(self, event: WorkflowEvent) -> None:
        self.history.append(event)
        if not self._handlers:
            return
        results = await asyncio.gather(
            *(h(event) for h in self._handlers), return_exceptions=True
        )
        # Surface nothing to the caller; subscriber errors must not abort a run.
        for r in results:
            if isinstance(r, Exception):  # pragma: no cover - defensive
                pass

    def clear(self) -> None:
        self.history.clear()
