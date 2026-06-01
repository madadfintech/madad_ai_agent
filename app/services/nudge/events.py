"""Nudge event framework.

Lifecycle events let Operational Visibility surface reminder activity and let
other services react (e.g. escalations routed to the ops queue). In-memory bus by
default; Redis Streams adapter is the production transport.
"""

from __future__ import annotations

import asyncio
from abc import ABC, abstractmethod
from collections.abc import Awaitable, Callable
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field

from app.shared.workflow.utils import new_id, utcnow


class NudgeEventType(StrEnum):
    SEQUENCE_STARTED = "nudge.sequence.started"
    SEQUENCE_COMPLETED = "nudge.sequence.completed"
    REMINDER_SCHEDULED = "nudge.reminder.scheduled"
    REMINDER_SENT = "nudge.reminder.sent"
    REMINDER_RETRYING = "nudge.reminder.retrying"
    REMINDER_FAILED = "nudge.reminder.failed"
    SEQUENCE_SUPPRESSED = "nudge.sequence.suppressed"
    SEQUENCE_ESCALATED = "nudge.sequence.escalated"
    SEQUENCE_CANCELLED = "nudge.sequence.cancelled"


class NudgeEvent(BaseModel):
    event_id: str = Field(default_factory=lambda: new_id("nevt"))
    type: NudgeEventType
    occurred_at: str = Field(default_factory=lambda: utcnow().isoformat())
    sequence_id: str
    reminder_id: str | None = None
    reason: str | None = None
    target_ref: str | None = None
    payload: dict[str, Any] = Field(default_factory=dict)


EventHandler = Callable[[NudgeEvent], Awaitable[None]]


class NudgeEventBus(ABC):
    @abstractmethod
    async def publish(self, event: NudgeEvent) -> None: ...

    @abstractmethod
    def subscribe(self, handler: EventHandler) -> None: ...


class InMemoryNudgeEventBus(NudgeEventBus):
    def __init__(self) -> None:
        self._handlers: list[EventHandler] = []
        self.history: list[NudgeEvent] = []

    def subscribe(self, handler: EventHandler) -> None:
        self._handlers.append(handler)

    async def publish(self, event: NudgeEvent) -> None:
        self.history.append(event)
        if self._handlers:
            await asyncio.gather(*(h(event) for h in self._handlers), return_exceptions=True)

    def clear(self) -> None:
        self.history.clear()
