"""Workflow audit logging.

Every meaningful runtime action (status transition, retry, timeout, recovery) is
recorded both as a durable :class:`AuditEntry` (via the run store) and as a
structured log line. This is what powers conversation replay and the operations
comms-review log required at Milestone 1.
"""

from __future__ import annotations

from typing import Any

from app.core.logging import get_logger

from .enums import RunStatus
from .persistence import AuditEntry, WorkflowRunStore


class AuditLogger:
    """Records audit entries to the run store and the structured log."""

    def __init__(self, run_store: WorkflowRunStore, logger: Any | None = None) -> None:
        self._store = run_store
        self._log = logger or get_logger("workflow.audit")

    async def record(
        self,
        run_id: str,
        action: str,
        *,
        from_status: RunStatus | None = None,
        to_status: RunStatus | None = None,
        detail: dict[str, Any] | None = None,
    ) -> AuditEntry:
        entry = AuditEntry(
            run_id=run_id,
            action=action,
            from_status=from_status,
            to_status=to_status,
            detail=detail or {},
        )
        await self._store.append_audit(entry)
        self._log.info(
            "workflow.audit",
            run_id=run_id,
            action=action,
            from_status=str(from_status) if from_status else None,
            to_status=str(to_status) if to_status else None,
            **(detail or {}),
        )
        return entry
