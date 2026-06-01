"""Unified event bus port + in-process default.

The :class:`EventBus` port is what producers depend on. The in-memory bus is the
default (zero external dependencies, used in tests/dev and as the single-process
transport). The Redis Streams bus in :mod:`app.shared.events.stream` is the
cross-process production transport.
"""

from __future__ import annotations

import asyncio
from abc import ABC, abstractmethod
from collections.abc import Awaitable, Callable

from .event import Event

EventHandler = Callable[[Event], Awaitable[None]]


class EventBus(ABC):
    """Port for publishing and (optionally) subscribing to unified events."""

    @abstractmethod
    async def publish(self, event: Event) -> None: ...

    @abstractmethod
    def subscribe(self, handler: EventHandler) -> None:
        """Register an in-process handler. Cross-process buses dispatch to these
        only while a consumer loop is draining their stream."""


class InMemoryEventBus(EventBus):
    """Default bus: keeps a history and fans out to in-process handlers.

    Handler failures are isolated so one bad subscriber cannot break a producer.
    """

    def __init__(self) -> None:
        self._handlers: list[EventHandler] = []
        self.history: list[Event] = []

    def subscribe(self, handler: EventHandler) -> None:
        self._handlers.append(handler)

    async def publish(self, event: Event) -> None:
        self.history.append(event)
        if not self._handlers:
            return
        await asyncio.gather(
            *(h(event) for h in self._handlers), return_exceptions=True
        )

    def clear(self) -> None:
        self.history.clear()
