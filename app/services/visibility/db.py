"""PostgreSQL-backed Operational Visibility activity store (audit schema)."""

from __future__ import annotations

from typing import Any

from sqlalchemy import String, select
from sqlalchemy.orm import Mapped, mapped_column

from app.shared.db import Base, Database

from .models import ActivityEvent
from .persistence import ActivityFilter, ActivityStore


class ActivityRow(Base):
    __tablename__ = "activities"
    __table_args__ = {"schema": "audit"}

    activity_id: Mapped[str] = mapped_column(String, primary_key=True)
    source: Mapped[str] = mapped_column(String, index=True)
    type: Mapped[str] = mapped_column(String, index=True)
    occurred_at: Mapped[str] = mapped_column(String, index=True)
    conversation_id: Mapped[str | None] = mapped_column(String, nullable=True, index=True)
    run_id: Mapped[str | None] = mapped_column(String, nullable=True, index=True)
    application_ref: Mapped[str | None] = mapped_column(String, nullable=True, index=True)
    identity: Mapped[str | None] = mapped_column(String, nullable=True, index=True)
    session_id: Mapped[str | None] = mapped_column(String, nullable=True, index=True)
    summary: Mapped[str | None] = mapped_column(String, nullable=True)
    data: Mapped[dict[str, Any]] = mapped_column()


class PostgresActivityStore(ActivityStore):
    def __init__(self, db: Database) -> None:
        self._db = db

    async def append(self, activity: ActivityEvent) -> ActivityEvent:
        async with self._db.session() as session:
            session.add(
                ActivityRow(
                    activity_id=activity.activity_id,
                    source=str(activity.source),
                    type=activity.type,
                    occurred_at=activity.occurred_at.isoformat(),
                    conversation_id=activity.conversation_id,
                    run_id=activity.run_id,
                    application_ref=activity.application_ref,
                    identity=activity.identity,
                    session_id=activity.session_id,
                    summary=activity.summary,
                    data=activity.model_dump(mode="json"),
                )
            )
        return activity

    async def query(
        self, filt: ActivityFilter, *, limit: int = 100, offset: int = 0
    ) -> list[ActivityEvent]:
        stmt = select(ActivityRow)
        if filt.source is not None:
            stmt = stmt.where(ActivityRow.source == str(filt.source))
        if filt.type is not None:
            stmt = stmt.where(ActivityRow.type == filt.type)
        if filt.conversation_id is not None:
            stmt = stmt.where(ActivityRow.conversation_id == filt.conversation_id)
        if filt.run_id is not None:
            stmt = stmt.where(ActivityRow.run_id == filt.run_id)
        if filt.application_ref is not None:
            stmt = stmt.where(ActivityRow.application_ref == filt.application_ref)
        if filt.identity is not None:
            stmt = stmt.where(ActivityRow.identity == filt.identity)
        if filt.session_id is not None:
            stmt = stmt.where(ActivityRow.session_id == filt.session_id)
        if filt.since is not None:
            stmt = stmt.where(ActivityRow.occurred_at >= filt.since.isoformat())
        if filt.until is not None:
            stmt = stmt.where(ActivityRow.occurred_at <= filt.until.isoformat())
        stmt = stmt.order_by(ActivityRow.occurred_at)

        if filt.text is None:
            # Common path: paginate in SQL so we never materialise the whole
            # filtered set (the activity log grows unbounded).
            stmt = stmt.offset(offset).limit(limit)
            async with self._db.session() as session:
                rows = (await session.execute(stmt)).scalars().all()
            return [ActivityEvent.model_validate(r.data) for r in rows]

        # Text search spans ``summary`` (inside the JSON ``data``), so filter in
        # Python then slice. Bounded admin-search path, not the hot read path.
        async with self._db.session() as session:
            rows = (await session.execute(stmt)).scalars().all()
        activities = [ActivityEvent.model_validate(r.data) for r in rows]
        needle = filt.text.lower()
        activities = [
            a for a in activities if needle in f"{a.type} {a.summary or ''}".lower()
        ]
        return activities[offset : offset + limit]

    async def all(self) -> list[ActivityEvent]:
        async with self._db.session() as session:
            rows = (
                await session.execute(select(ActivityRow).order_by(ActivityRow.occurred_at))
            ).scalars().all()
            return [ActivityEvent.model_validate(r.data) for r in rows]
