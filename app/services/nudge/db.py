"""PostgreSQL-backed nudge stores (sequence + reminder)."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import String, select
from sqlalchemy.orm import Mapped, mapped_column

from app.shared.db import Base, Database

from .enums import REMINDER_DUE_STATUSES, SEQUENCE_TERMINAL
from .models import NudgeSequence, Reminder
from .persistence import NudgeStore


class NudgeSequenceRow(Base):
    __tablename__ = "sequences"
    __table_args__ = {"schema": "nudge"}

    sequence_id: Mapped[str] = mapped_column(String, primary_key=True)
    reason: Mapped[str] = mapped_column(String, index=True)
    target_ref: Mapped[str | None] = mapped_column(String, nullable=True, index=True)
    status: Mapped[str] = mapped_column(String, index=True)
    data: Mapped[dict[str, Any]] = mapped_column()


class ReminderRow(Base):
    __tablename__ = "reminders"
    __table_args__ = {"schema": "nudge"}

    reminder_id: Mapped[str] = mapped_column(String, primary_key=True)
    sequence_id: Mapped[str] = mapped_column(String, index=True)
    status: Mapped[str] = mapped_column(String, index=True)
    scheduled_for: Mapped[str] = mapped_column(String, index=True)
    step_index: Mapped[int] = mapped_column(index=True)
    data: Mapped[dict[str, Any]] = mapped_column()


class PostgresNudgeStore(NudgeStore):
    def __init__(self, db: Database) -> None:
        self._db = db

    # -- sequences ------------------------------------------------------------

    async def create_sequence(self, sequence: NudgeSequence) -> NudgeSequence:
        async with self._db.session() as session:
            session.add(_sequence_row(sequence))
        return sequence

    async def get_sequence(self, sequence_id: str) -> NudgeSequence | None:
        async with self._db.session() as session:
            row = await session.get(NudgeSequenceRow, sequence_id)
            return NudgeSequence.model_validate(row.data) if row else None

    async def save_sequence(self, sequence: NudgeSequence) -> NudgeSequence:
        from app.shared.workflow.utils import utcnow

        sequence.updated_at = utcnow()
        async with self._db.session() as session:
            row = await session.get(NudgeSequenceRow, sequence.sequence_id)
            if row is None:
                session.add(_sequence_row(sequence))
            else:
                row.status = str(sequence.status)
                row.data = sequence.model_dump(mode="json")
        return sequence

    async def find_active_sequence(
        self, reason: str, target_ref: str
    ) -> NudgeSequence | None:
        active = [str(s) for s in SEQUENCE_TERMINAL]
        async with self._db.session() as session:
            row = (
                await session.execute(
                    select(NudgeSequenceRow).where(
                        NudgeSequenceRow.reason == reason,
                        NudgeSequenceRow.target_ref == target_ref,
                        NudgeSequenceRow.status.not_in(active),
                    )
                )
            ).scalars().first()
            return NudgeSequence.model_validate(row.data) if row else None

    async def list_active_sequences(
        self, *, target_ref: str | None = None, identity: str | None = None
    ) -> list[NudgeSequence]:
        terminal = [str(s) for s in SEQUENCE_TERMINAL]
        stmt = select(NudgeSequenceRow).where(NudgeSequenceRow.status.not_in(terminal))
        if target_ref is not None:
            stmt = stmt.where(NudgeSequenceRow.target_ref == target_ref)
        async with self._db.session() as session:
            rows = (await session.execute(stmt)).scalars().all()
        sequences = [NudgeSequence.model_validate(r.data) for r in rows]
        if identity is not None:
            sequences = [s for s in sequences if identity in s.targets.values()]
        return sequences

    # -- reminders ------------------------------------------------------------

    async def create_reminder(self, reminder: Reminder) -> Reminder:
        async with self._db.session() as session:
            session.add(_reminder_row(reminder))
        return reminder

    async def get_reminder(self, reminder_id: str) -> Reminder | None:
        async with self._db.session() as session:
            row = await session.get(ReminderRow, reminder_id)
            return Reminder.model_validate(row.data) if row else None

    async def save_reminder(self, reminder: Reminder) -> Reminder:
        from app.shared.workflow.utils import utcnow

        reminder.updated_at = utcnow()
        async with self._db.session() as session:
            row = await session.get(ReminderRow, reminder.reminder_id)
            if row is None:
                session.add(_reminder_row(reminder))
            else:
                row.status = str(reminder.status)
                row.scheduled_for = reminder.scheduled_for.isoformat()
                row.data = reminder.model_dump(mode="json")
        return reminder

    async def list_due(self, now: datetime, limit: int = 100) -> list[Reminder]:
        due = [str(s) for s in REMINDER_DUE_STATUSES]
        async with self._db.session() as session:
            rows = (
                await session.execute(
                    select(ReminderRow)
                    .where(
                        ReminderRow.status.in_(due),
                        ReminderRow.scheduled_for <= now.isoformat(),
                    )
                    .order_by(ReminderRow.scheduled_for)
                    .limit(limit)
                )
            ).scalars().all()
            return [Reminder.model_validate(r.data) for r in rows]

    async def list_pending_by_sequence(self, sequence_id: str) -> list[Reminder]:
        pending = [str(s) for s in REMINDER_DUE_STATUSES]
        async with self._db.session() as session:
            rows = (
                await session.execute(
                    select(ReminderRow).where(
                        ReminderRow.sequence_id == sequence_id,
                        ReminderRow.status.in_(pending),
                    )
                )
            ).scalars().all()
            return [Reminder.model_validate(r.data) for r in rows]

    async def list_by_sequence(self, sequence_id: str) -> list[Reminder]:
        async with self._db.session() as session:
            rows = (
                await session.execute(
                    select(ReminderRow)
                    .where(ReminderRow.sequence_id == sequence_id)
                    .order_by(ReminderRow.step_index)
                )
            ).scalars().all()
            return [Reminder.model_validate(r.data) for r in rows]


def _sequence_row(sequence: NudgeSequence) -> NudgeSequenceRow:
    return NudgeSequenceRow(
        sequence_id=sequence.sequence_id,
        reason=sequence.reason,
        target_ref=sequence.target_ref,
        status=str(sequence.status),
        data=sequence.model_dump(mode="json"),
    )


def _reminder_row(reminder: Reminder) -> ReminderRow:
    return ReminderRow(
        reminder_id=reminder.reminder_id,
        sequence_id=reminder.sequence_id,
        status=str(reminder.status),
        scheduled_for=reminder.scheduled_for.isoformat(),
        step_index=reminder.step_index,
        data=reminder.model_dump(mode="json"),
    )
