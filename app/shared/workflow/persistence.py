"""Workflow run persistence — the durable operational record of a run.

This is distinct from LangGraph *checkpointing*: checkpoints store the graph's
internal channel values for resume; this run store is our own operational view
(status, current step, attempts, last error, timing) that the recovery engine,
nudge service, and dashboards read.

The in-memory store is the default. A Postgres-backed store (``workflow`` schema)
will be added with the platform DB foundation; the port keeps the executor
agnostic to which one is wired.
"""

from __future__ import annotations

import asyncio
from abc import ABC, abstractmethod
from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field

from .enums import RECOVERABLE_STATUSES, Channel, RunStatus
from .errors import RunNotFoundError
from .utils import new_id, utcnow


class AuditEntry(BaseModel):
    """One immutable audit record for a run (transition or execution event)."""

    entry_id: str = Field(default_factory=lambda: new_id("aud"))
    run_id: str
    at: datetime = Field(default_factory=utcnow)
    action: str
    from_status: RunStatus | None = None
    to_status: RunStatus | None = None
    detail: dict[str, Any] = Field(default_factory=dict)


class WorkflowRun(BaseModel):
    """Durable operational record of a single workflow instance."""

    run_id: str = Field(default_factory=lambda: new_id("run"))
    workflow: str
    version: int = 1
    session_id: str
    thread_id: str
    channel: Channel | None = None
    identity: str = ""

    status: RunStatus = RunStatus.PENDING
    current_step: str | None = None
    attempts: int = 0
    last_error: str | None = None
    # Pending interrupt payload(s) when the run is waiting for input.
    pending_interrupts: list[dict[str, Any]] = Field(default_factory=list)

    correlation_id: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)

    created_at: datetime = Field(default_factory=utcnow)
    updated_at: datetime = Field(default_factory=utcnow)
    completed_at: datetime | None = None
    # When a waiting session should be considered lapsed (drives timeout sweeps).
    expires_at: datetime | None = None


class WorkflowRunStore(ABC):
    """Port for persisting workflow runs and their audit trail."""

    @abstractmethod
    async def create(self, run: WorkflowRun) -> WorkflowRun: ...

    @abstractmethod
    async def get(self, run_id: str) -> WorkflowRun: ...

    @abstractmethod
    async def get_or_none(self, run_id: str) -> WorkflowRun | None: ...

    @abstractmethod
    async def save(self, run: WorkflowRun) -> WorkflowRun: ...

    @abstractmethod
    async def list_by_status(self, *statuses: RunStatus) -> list[WorkflowRun]: ...

    @abstractmethod
    async def list_recoverable(self, limit: int = 100) -> list[WorkflowRun]: ...

    @abstractmethod
    async def list_waiting_expired(
        self, now: datetime, limit: int = 100
    ) -> list[WorkflowRun]: ...

    @abstractmethod
    async def append_audit(self, entry: AuditEntry) -> None: ...

    @abstractmethod
    async def list_audit(self, run_id: str) -> list[AuditEntry]: ...


class InMemoryWorkflowRunStore(WorkflowRunStore):
    """Thread/async-safe in-memory implementation for tests and dev."""

    def __init__(self) -> None:
        self._runs: dict[str, WorkflowRun] = {}
        self._audit: dict[str, list[AuditEntry]] = {}
        self._lock = asyncio.Lock()

    async def create(self, run: WorkflowRun) -> WorkflowRun:
        async with self._lock:
            self._runs[run.run_id] = run.model_copy(deep=True)
        return run

    async def get(self, run_id: str) -> WorkflowRun:
        run = await self.get_or_none(run_id)
        if run is None:
            raise RunNotFoundError(f"Run {run_id!r} not found")
        return run

    async def get_or_none(self, run_id: str) -> WorkflowRun | None:
        async with self._lock:
            stored = self._runs.get(run_id)
            return stored.model_copy(deep=True) if stored else None

    async def save(self, run: WorkflowRun) -> WorkflowRun:
        run.updated_at = utcnow()
        async with self._lock:
            self._runs[run.run_id] = run.model_copy(deep=True)
        return run

    async def list_by_status(self, *statuses: RunStatus) -> list[WorkflowRun]:
        wanted = set(statuses)
        async with self._lock:
            return [
                r.model_copy(deep=True)
                for r in self._runs.values()
                if r.status in wanted
            ]

    async def list_recoverable(self, limit: int = 100) -> list[WorkflowRun]:
        async with self._lock:
            runs = [
                r.model_copy(deep=True)
                for r in self._runs.values()
                if r.status in RECOVERABLE_STATUSES
            ]
        runs.sort(key=lambda r: r.updated_at)
        return runs[:limit]

    async def list_waiting_expired(
        self, now: datetime, limit: int = 100
    ) -> list[WorkflowRun]:
        async with self._lock:
            runs = [
                r.model_copy(deep=True)
                for r in self._runs.values()
                if r.status == RunStatus.WAITING_FOR_INPUT
                and r.expires_at is not None
                and r.expires_at <= now
            ]
        runs.sort(key=lambda r: r.expires_at or now)
        return runs[:limit]

    async def append_audit(self, entry: AuditEntry) -> None:
        async with self._lock:
            self._audit.setdefault(entry.run_id, []).append(entry.model_copy(deep=True))

    async def list_audit(self, run_id: str) -> list[AuditEntry]:
        async with self._lock:
            return [e.model_copy(deep=True) for e in self._audit.get(run_id, [])]
