"""Document Intelligence event framework.

Lifecycle events let the workflow advance (e.g. a checklist update unblocks the
next onboarding step) and let Operational Visibility surface document processing.
In-memory bus by default; Redis Streams adapter is the production transport.
"""

from __future__ import annotations

import asyncio
from abc import ABC, abstractmethod
from collections.abc import Awaitable, Callable
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field

from app.shared.workflow.utils import new_id, utcnow


class DocumentEventType(StrEnum):
    DOCUMENT_RECEIVED = "document.received"
    DOCUMENT_REJECTED = "document.rejected"  # failed Madad validation
    DOCUMENT_COMPLETED = "document.completed"  # stored + validated at Madad
    DOCUMENT_FAILED = "document.failed"  # routing exhausted retries
    DOCUMENT_RETRYING = "document.retrying"
    ZIP_EXTRACTED = "document.zip.extracted"
    CHECKLIST_UPDATED = "document.checklist.updated"


class DocumentEvent(BaseModel):
    event_id: str = Field(default_factory=lambda: new_id("devt"))
    type: DocumentEventType
    occurred_at: str = Field(default_factory=lambda: utcnow().isoformat())
    document_id: str | None = None
    batch_id: str | None = None
    application_ref: str | None = None
    payload: dict[str, Any] = Field(default_factory=dict)


EventHandler = Callable[[DocumentEvent], Awaitable[None]]


class DocumentEventBus(ABC):
    @abstractmethod
    async def publish(self, event: DocumentEvent) -> None: ...

    @abstractmethod
    def subscribe(self, handler: EventHandler) -> None: ...


class InMemoryDocumentEventBus(DocumentEventBus):
    def __init__(self) -> None:
        self._handlers: list[EventHandler] = []
        self.history: list[DocumentEvent] = []

    def subscribe(self, handler: EventHandler) -> None:
        self._handlers.append(handler)

    async def publish(self, event: DocumentEvent) -> None:
        self.history.append(event)
        if self._handlers:
            await asyncio.gather(*(h(event) for h in self._handlers), return_exceptions=True)

    def clear(self) -> None:
        self.history.clear()
