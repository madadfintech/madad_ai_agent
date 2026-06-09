"""PostgreSQL-backed workflow run store (JSON-document pattern).

Implements :class:`WorkflowRunStore` durably so workflow runs + audit survive
restarts and are visible across instances. The LangGraph checkpointer (graph
channel values) is persisted separately by ``PostgresCheckpointerProvider``.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import String, delete, select
from sqlalchemy.orm import Mapped, mapped_column

from app.shared.db import Base, Database
from app.shared.workflow.utils import utcnow

from ..enums import RECOVERABLE_STATUSES, RunStatus
from ..errors import RunNotFoundError
from ..persistence import AuditEntry, WorkflowRun, WorkflowRunStore


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value is not None else None


class WorkflowRunRow(Base):
    __tablename__ = "runs"
    __table_args__ = {"schema": "workflow"}

    run_id: Mapped[str] = mapped_column(String, primary_key=True)
    status: Mapped[str] = mapped_column(String, index=True)
    expires_at: Mapped[str | None] = mapped_column(String, nullable=True, index=True)
    updated_at: Mapped[str] = mapped_column(String, index=True)
    data: Mapped[dict[str, Any]] = mapped_column()


class WorkflowRunAuditRow(Base):
    __tablename__ = "run_audit"
    __table_args__ = {"schema": "workflow"}

    entry_id: Mapped[str] = mapped_column(String, primary_key=True)
    run_id: Mapped[str] = mapped_column(String, index=True)
    at: Mapped[str] = mapped_column(String, index=True)
    data: Mapped[dict[str, Any]] = mapped_column()


class PostgresWorkflowRunStore(WorkflowRunStore):
    def __init__(self, db: Database) -> None:
        self._db = db

    async def create(self, run: WorkflowRun) -> WorkflowRun:
        async with self._db.session() as session:
            session.add(_row(run))
        return run

    async def get(self, run_id: str) -> WorkflowRun:
        run = await self.get_or_none(run_id)
        if run is None:
            raise RunNotFoundError(f"Run {run_id!r} not found")
        return run

    async def get_or_none(self, run_id: str) -> WorkflowRun | None:
        async with self._db.session() as session:
            row = await session.get(WorkflowRunRow, run_id)
            return WorkflowRun.model_validate(row.data) if row else None

    async def save(self, run: WorkflowRun) -> WorkflowRun:
        run.updated_at = utcnow()
        async with self._db.session() as session:
            row = await session.get(WorkflowRunRow, run.run_id)
            if row is None:
                session.add(_row(run))
            else:
                row.status = str(run.status)
                row.expires_at = _iso(run.expires_at)
                row.updated_at = run.updated_at.isoformat()
                row.data = run.model_dump(mode="json")
        return run

    async def list_by_status(self, *statuses: RunStatus) -> list[WorkflowRun]:
        wanted = [str(s) for s in statuses]
        return await self._select(WorkflowRunRow.status.in_(wanted))

    async def list_recoverable(self, limit: int = 100) -> list[WorkflowRun]:
        wanted = [str(s) for s in RECOVERABLE_STATUSES]
        return await self._select(
            WorkflowRunRow.status.in_(wanted),
            order_by=WorkflowRunRow.updated_at,
            limit=limit,
        )

    async def list_waiting_expired(
        self, now: datetime, limit: int = 100
    ) -> list[WorkflowRun]:
        cutoff = now.isoformat()
        return await self._select(
            WorkflowRunRow.status == str(RunStatus.WAITING_FOR_INPUT),
            WorkflowRunRow.expires_at.is_not(None),
            WorkflowRunRow.expires_at <= cutoff,
            order_by=WorkflowRunRow.expires_at,
            limit=limit,
        )

    async def append_audit(self, entry: AuditEntry) -> None:
        async with self._db.session() as session:
            session.add(
                WorkflowRunAuditRow(
                    entry_id=entry.entry_id,
                    run_id=entry.run_id,
                    at=entry.at.isoformat(),
                    data=entry.model_dump(mode="json"),
                )
            )

    async def delete_by_session(self, session_id: str) -> list[str]:
        async with self._db.session() as session:
            # Load all rows + filter in Python — the JSON column isn't
            # mapped as JSONB so a `data["session_id"].astext` predicate
            # is rejected by SQLAlchemy. Admin reset is one-shot, so a
            # full scan is fine; pulls the matching subset, captures
            # their thread ids for checkpoint cleanup, then deletes the
            # rows + their audit trail in the same transaction.
            all_rows = (
                await session.execute(select(WorkflowRunRow))
            ).scalars().all()
            run_ids: list[str] = []
            thread_ids: list[str] = []
            for row in all_rows:
                data = row.data if isinstance(row.data, dict) else {}
                if data.get("session_id") != session_id:
                    continue
                run_ids.append(row.run_id)
                tid = data.get("thread_id")
                if isinstance(tid, str) and tid:
                    thread_ids.append(tid)
            if not run_ids:
                return []
            await session.execute(
                delete(WorkflowRunAuditRow).where(
                    WorkflowRunAuditRow.run_id.in_(run_ids)
                )
            )
            await session.execute(
                delete(WorkflowRunRow).where(WorkflowRunRow.run_id.in_(run_ids))
            )
        return thread_ids

    async def list_audit(self, run_id: str) -> list[AuditEntry]:
        async with self._db.session() as session:
            rows = (
                await session.execute(
                    select(WorkflowRunAuditRow)
                    .where(WorkflowRunAuditRow.run_id == run_id)
                    .order_by(WorkflowRunAuditRow.at)
                )
            ).scalars().all()
            return [AuditEntry.model_validate(r.data) for r in rows]

    async def _select(
        self, *where: Any, order_by: Any = None, limit: int | None = None
    ) -> list[WorkflowRun]:
        stmt = select(WorkflowRunRow).where(*where)
        if order_by is not None:
            stmt = stmt.order_by(order_by)
        if limit is not None:
            stmt = stmt.limit(limit)
        async with self._db.session() as session:
            rows = (await session.execute(stmt)).scalars().all()
            return [WorkflowRun.model_validate(r.data) for r in rows]


def _row(run: WorkflowRun) -> WorkflowRunRow:
    return WorkflowRunRow(
        run_id=run.run_id,
        status=str(run.status),
        expires_at=_iso(run.expires_at),
        updated_at=run.updated_at.isoformat(),
        data=run.model_dump(mode="json"),
    )
