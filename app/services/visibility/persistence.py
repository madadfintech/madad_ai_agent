"""Activity log persistence + filtering.

The activity log is the read-optimized projection that powers traceability,
audit visibility, and search. In-memory now; an OpenSearch/Postgres-backed store
lands with the platform infra (the port keeps the service agnostic).
"""

from __future__ import annotations

import asyncio
from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import datetime

from .enums import ActivitySource
from .models import ActivityEvent


@dataclass
class ActivityFilter:
    """Search/filter criteria for the activity log."""

    source: ActivitySource | None = None
    type: str | None = None
    session_id: str | None = None
    conversation_id: str | None = None
    run_id: str | None = None
    application_ref: str | None = None
    identity: str | None = None
    since: datetime | None = None
    until: datetime | None = None
    text: str | None = None  # substring match over type/summary

    def matches(self, activity: ActivityEvent) -> bool:
        if self.source is not None and activity.source != self.source:
            return False
        if self.type is not None and activity.type != self.type:
            return False
        if self.session_id is not None and activity.session_id != self.session_id:
            return False
        if self.conversation_id is not None and activity.conversation_id != self.conversation_id:
            return False
        if self.run_id is not None and activity.run_id != self.run_id:
            return False
        if self.application_ref is not None and activity.application_ref != self.application_ref:
            return False
        if self.identity is not None and activity.identity != self.identity:
            return False
        if self.since is not None and activity.occurred_at < self.since:
            return False
        if self.until is not None and activity.occurred_at > self.until:
            return False
        if self.text is not None:
            haystack = f"{activity.type} {activity.summary or ''}".lower()
            if self.text.lower() not in haystack:
                return False
        return True


class ActivityStore(ABC):
    @abstractmethod
    async def append(self, activity: ActivityEvent) -> ActivityEvent: ...

    @abstractmethod
    async def query(
        self, filt: ActivityFilter, *, limit: int = 100, offset: int = 0
    ) -> list[ActivityEvent]: ...

    @abstractmethod
    async def all(self) -> list[ActivityEvent]: ...


class InMemoryActivityStore(ActivityStore):
    def __init__(self) -> None:
        self._activities: list[ActivityEvent] = []
        self._lock = asyncio.Lock()

    async def append(self, activity: ActivityEvent) -> ActivityEvent:
        async with self._lock:
            self._activities.append(activity.model_copy(deep=True))
        return activity

    async def query(
        self, filt: ActivityFilter, *, limit: int = 100, offset: int = 0
    ) -> list[ActivityEvent]:
        async with self._lock:
            matched = [a.model_copy(deep=True) for a in self._activities if filt.matches(a)]
        matched.sort(key=lambda a: a.occurred_at)
        return matched[offset : offset + limit]

    async def all(self) -> list[ActivityEvent]:
        async with self._lock:
            return [a.model_copy(deep=True) for a in self._activities]
