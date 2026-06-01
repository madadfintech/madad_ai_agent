"""CMS audit logging — who changed what config, when.

Operational config changes are sensitive (they alter live conversations), so
every write/rollback/delete is recorded for the operations review trail.
"""

from __future__ import annotations

import asyncio
from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field

from app.core.logging import get_logger
from app.shared.workflow.utils import new_id, utcnow

from .enums import ConfigKind


class CmsAuditEntry(BaseModel):
    entry_id: str = Field(default_factory=lambda: new_id("cmsaud"))
    at: datetime = Field(default_factory=utcnow)
    action: str
    kind: ConfigKind | None = None
    name: str | None = None
    version: int | None = None
    updated_by: str | None = None
    detail: dict[str, Any] = Field(default_factory=dict)


class CmsAuditLogger:
    def __init__(self, logger: Any | None = None) -> None:
        self._entries: list[CmsAuditEntry] = []
        self._lock = asyncio.Lock()
        self._log = logger or get_logger("cms.audit")

    async def record(
        self,
        action: str,
        *,
        kind: ConfigKind | None = None,
        name: str | None = None,
        version: int | None = None,
        updated_by: str | None = None,
        detail: dict[str, Any] | None = None,
    ) -> CmsAuditEntry:
        entry = CmsAuditEntry(
            action=action,
            kind=kind,
            name=name,
            version=version,
            updated_by=updated_by,
            detail=detail or {},
        )
        async with self._lock:
            self._entries.append(entry)
        self._log.info(
            "cms.audit",
            action=action,
            kind=str(kind) if kind else None,
            name=name,
            version=version,
            updated_by=updated_by,
        )
        return entry

    async def list_entries(self, *, name: str | None = None) -> list[CmsAuditEntry]:
        async with self._lock:
            return [
                e.model_copy(deep=True)
                for e in self._entries
                if name is None or e.name == name
            ]
