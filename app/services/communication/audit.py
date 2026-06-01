"""Communication audit logging.

Records an immutable trail of message lifecycle actions for the operations
comms-review log (a Milestone 1 requirement). Entries are persisted via an
in-memory store by default (Postgres ``communication`` schema later) and also
emitted to the structured log.
"""

from __future__ import annotations

import asyncio
from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field

from app.core.logging import get_logger
from app.shared.workflow.utils import new_id, utcnow


class CommunicationAuditEntry(BaseModel):
    entry_id: str = Field(default_factory=lambda: new_id("caud"))
    at: datetime = Field(default_factory=utcnow)
    conversation_id: str
    message_id: str | None = None
    action: str
    detail: dict[str, Any] = Field(default_factory=dict)


class CommunicationAuditLogger:
    """Persists audit entries and mirrors them to the structured log."""

    def __init__(self, logger: Any | None = None) -> None:
        self._entries: list[CommunicationAuditEntry] = []
        self._lock = asyncio.Lock()
        self._log = logger or get_logger("communication.audit")

    async def record(
        self,
        conversation_id: str,
        action: str,
        *,
        message_id: str | None = None,
        detail: dict[str, Any] | None = None,
    ) -> CommunicationAuditEntry:
        entry = CommunicationAuditEntry(
            conversation_id=conversation_id,
            message_id=message_id,
            action=action,
            detail=detail or {},
        )
        async with self._lock:
            self._entries.append(entry)
        self._log.info(
            "communication.audit",
            conversation_id=conversation_id,
            message_id=message_id,
            action=action,
            **(detail or {}),
        )
        return entry

    async def list_for_conversation(self, conversation_id: str) -> list[CommunicationAuditEntry]:
        async with self._lock:
            return [
                e.model_copy(deep=True)
                for e in self._entries
                if e.conversation_id == conversation_id
            ]
