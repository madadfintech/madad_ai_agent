"""Nudge persistence — the durable store of sequences and scheduled reminders.

The reminder store is effectively the delayed-job queue: ``list_due`` is what the
worker tick drains. In-memory now; Postgres (orchestration state) + Redis/Celery
queues land with the platform infra.
"""

from __future__ import annotations

import asyncio
from abc import ABC, abstractmethod
from datetime import datetime

from app.shared.workflow.utils import utcnow

from .enums import REMINDER_DUE_STATUSES, SEQUENCE_TERMINAL
from .models import NudgeSequence, Reminder


class NudgeStore(ABC):
    # sequences
    @abstractmethod
    async def create_sequence(self, sequence: NudgeSequence) -> NudgeSequence: ...

    @abstractmethod
    async def get_sequence(self, sequence_id: str) -> NudgeSequence | None: ...

    @abstractmethod
    async def save_sequence(self, sequence: NudgeSequence) -> NudgeSequence: ...

    @abstractmethod
    async def find_active_sequence(
        self, reason: str, target_ref: str
    ) -> NudgeSequence | None: ...

    @abstractmethod
    async def list_active_sequences(
        self, *, target_ref: str | None = None, identity: str | None = None
    ) -> list[NudgeSequence]: ...

    # reminders
    @abstractmethod
    async def create_reminder(self, reminder: Reminder) -> Reminder: ...

    @abstractmethod
    async def get_reminder(self, reminder_id: str) -> Reminder | None: ...

    @abstractmethod
    async def save_reminder(self, reminder: Reminder) -> Reminder: ...

    @abstractmethod
    async def list_due(self, now: datetime, limit: int = 100) -> list[Reminder]: ...

    @abstractmethod
    async def list_pending_by_sequence(self, sequence_id: str) -> list[Reminder]: ...

    @abstractmethod
    async def list_by_sequence(self, sequence_id: str) -> list[Reminder]: ...


class InMemoryNudgeStore(NudgeStore):
    def __init__(self) -> None:
        self._sequences: dict[str, NudgeSequence] = {}
        self._reminders: dict[str, Reminder] = {}
        self._lock = asyncio.Lock()

    async def create_sequence(self, sequence: NudgeSequence) -> NudgeSequence:
        async with self._lock:
            self._sequences[sequence.sequence_id] = sequence.model_copy(deep=True)
        return sequence

    async def get_sequence(self, sequence_id: str) -> NudgeSequence | None:
        async with self._lock:
            stored = self._sequences.get(sequence_id)
            return stored.model_copy(deep=True) if stored else None

    async def save_sequence(self, sequence: NudgeSequence) -> NudgeSequence:
        sequence.updated_at = utcnow()
        async with self._lock:
            self._sequences[sequence.sequence_id] = sequence.model_copy(deep=True)
        return sequence

    async def find_active_sequence(
        self, reason: str, target_ref: str
    ) -> NudgeSequence | None:
        async with self._lock:
            for seq in self._sequences.values():
                if (
                    seq.reason == reason
                    and seq.target_ref == target_ref
                    and seq.status not in SEQUENCE_TERMINAL
                ):
                    return seq.model_copy(deep=True)
            return None

    async def list_active_sequences(
        self, *, target_ref: str | None = None, identity: str | None = None
    ) -> list[NudgeSequence]:
        async with self._lock:
            result = []
            for seq in self._sequences.values():
                if seq.status in SEQUENCE_TERMINAL:
                    continue
                if target_ref is not None and seq.target_ref != target_ref:
                    continue
                if identity is not None and identity not in seq.targets.values():
                    continue
                result.append(seq.model_copy(deep=True))
            return result

    async def create_reminder(self, reminder: Reminder) -> Reminder:
        async with self._lock:
            self._reminders[reminder.reminder_id] = reminder.model_copy(deep=True)
        return reminder

    async def get_reminder(self, reminder_id: str) -> Reminder | None:
        async with self._lock:
            stored = self._reminders.get(reminder_id)
            return stored.model_copy(deep=True) if stored else None

    async def save_reminder(self, reminder: Reminder) -> Reminder:
        reminder.updated_at = utcnow()
        async with self._lock:
            self._reminders[reminder.reminder_id] = reminder.model_copy(deep=True)
        return reminder

    async def list_due(self, now: datetime, limit: int = 100) -> list[Reminder]:
        async with self._lock:
            due = [
                r.model_copy(deep=True)
                for r in self._reminders.values()
                if r.status in REMINDER_DUE_STATUSES and r.scheduled_for <= now
            ]
        due.sort(key=lambda r: r.scheduled_for)
        return due[:limit]

    async def list_pending_by_sequence(self, sequence_id: str) -> list[Reminder]:
        async with self._lock:
            return [
                r.model_copy(deep=True)
                for r in self._reminders.values()
                if r.sequence_id == sequence_id and r.status in REMINDER_DUE_STATUSES
            ]

    async def list_by_sequence(self, sequence_id: str) -> list[Reminder]:
        async with self._lock:
            reminders = [
                r.model_copy(deep=True)
                for r in self._reminders.values()
                if r.sequence_id == sequence_id
            ]
        reminders.sort(key=lambda r: r.step_index)
        return reminders


def is_pending(reminder: Reminder) -> bool:
    return reminder.status in REMINDER_DUE_STATUSES
