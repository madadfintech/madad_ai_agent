"""Communication event framework.

The service emits lifecycle events (message received/sent/delivered/failed,
attachment received) so other services react without coupling to the service
internals — e.g. the Workflow service resumes a conversation on MESSAGE_RECEIVED,
and Document Intelligence picks up ATTACHMENT_RECEIVED.

In-memory bus is the default; a Redis Streams adapter mirrors the workflow
runtime's and is the production transport.
"""

from __future__ import annotations

import asyncio
from abc import ABC, abstractmethod
from collections.abc import Awaitable, Callable
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field

from app.shared.workflow.enums import Channel
from app.shared.workflow.utils import new_id, utcnow


class CommunicationEventType(StrEnum):
    MESSAGE_RECEIVED = "communication.message.received"
    MESSAGE_QUEUED = "communication.message.queued"
    MESSAGE_SENT = "communication.message.sent"
    MESSAGE_DELIVERED = "communication.message.delivered"
    MESSAGE_READ = "communication.message.read"
    MESSAGE_FAILED = "communication.message.failed"
    MESSAGE_RETRYING = "communication.message.retrying"
    ATTACHMENT_RECEIVED = "communication.attachment.received"
    CONVERSATION_OPENED = "communication.conversation.opened"


class CommunicationEvent(BaseModel):
    event_id: str = Field(default_factory=lambda: new_id("cevt"))
    type: CommunicationEventType
    occurred_at: str = Field(default_factory=lambda: utcnow().isoformat())
    conversation_id: str
    message_id: str | None = None
    channel: Channel | None = None
    identity: str = ""
    correlation_id: str | None = None
    payload: dict[str, Any] = Field(default_factory=dict)


EventHandler = Callable[[CommunicationEvent], Awaitable[None]]


class CommunicationEventBus(ABC):
    @abstractmethod
    async def publish(self, event: CommunicationEvent) -> None: ...

    @abstractmethod
    def subscribe(self, handler: EventHandler) -> None: ...


class InMemoryCommunicationEventBus(CommunicationEventBus):
    """Default bus: keeps history and fans out to in-process handlers."""

    def __init__(self) -> None:
        self._handlers: list[EventHandler] = []
        self.history: list[CommunicationEvent] = []

    def subscribe(self, handler: EventHandler) -> None:
        self._handlers.append(handler)

    async def publish(self, event: CommunicationEvent) -> None:
        self.history.append(event)
        if self._handlers:
            await asyncio.gather(*(h(event) for h in self._handlers), return_exceptions=True)

    def clear(self) -> None:
        self.history.clear()
