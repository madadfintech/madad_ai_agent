"""Nudge audit logging — a trail of every reminder action for ops review."""

from __future__ import annotations

import asyncio
from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field

from app.core.logging import get_logger
from app.shared.workflow.utils import new_id, utcnow


class NudgeAuditEntry(BaseModel):
    entry_id: str = Field(default_factory=lambda: new_id("naud"))
    at: datetime = Field(default_factory=utcnow)
    sequence_id: str
    reminder_id: str | None = None
    action: str
    detail: dict[str, Any] = Field(default_factory=dict)


class NudgeAuditLogger:
    def __init__(self, logger: Any | None = None) -> None:
        self._entries: list[NudgeAuditEntry] = []
        self._lock = asyncio.Lock()
        self._log = logger or get_logger("nudge.audit")

    async def record(
        self,
        sequence_id: str,
        action: str,
        *,
        reminder_id: str | None = None,
        detail: dict[str, Any] | None = None,
    ) -> NudgeAuditEntry:
        entry = NudgeAuditEntry(
            sequence_id=sequence_id,
            reminder_id=reminder_id,
            action=action,
            detail=detail or {},
        )
        async with self._lock:
            self._entries.append(entry)
        self._log.info(
            "nudge.audit",
            sequence_id=sequence_id,
            reminder_id=reminder_id,
            action=action,
            **(detail or {}),
        )
        return entry

    async def list_for_sequence(self, sequence_id: str) -> list[NudgeAuditEntry]:
        async with self._lock:
            return [
                e.model_copy(deep=True)
                for e in self._entries
                if e.sequence_id == sequence_id
            ]
