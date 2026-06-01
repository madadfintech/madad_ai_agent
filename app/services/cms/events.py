"""CMS event framework.

Config changes emit events so other services can react (e.g. Operational
Visibility logging config changes, or a Redis-backed cache invalidator fanning
out evictions across instances). In-memory bus is the default; a Redis Streams
adapter is the production transport.
"""

from __future__ import annotations

import asyncio
from abc import ABC, abstractmethod
from collections.abc import Awaitable, Callable
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field

from app.shared.i18n import Locale
from app.shared.workflow.enums import Channel
from app.shared.workflow.utils import new_id, utcnow

from .enums import ConfigKind


class CmsEventType(StrEnum):
    CONFIG_UPDATED = "cms.config.updated"
    CONFIG_ROLLED_BACK = "cms.config.rolled_back"
    CONFIG_DELETED = "cms.config.deleted"
    CONFIG_REFRESHED = "cms.config.refreshed"


class CmsEvent(BaseModel):
    event_id: str = Field(default_factory=lambda: new_id("cmsevt"))
    type: CmsEventType
    occurred_at: str = Field(default_factory=lambda: utcnow().isoformat())
    kind: ConfigKind | None = None
    name: str | None = None
    channel: Channel | None = None
    locale: Locale | None = None
    version: int | None = None
    payload: dict[str, Any] = Field(default_factory=dict)


EventHandler = Callable[[CmsEvent], Awaitable[None]]


class CmsEventBus(ABC):
    @abstractmethod
    async def publish(self, event: CmsEvent) -> None: ...

    @abstractmethod
    def subscribe(self, handler: EventHandler) -> None: ...


class InMemoryCmsEventBus(CmsEventBus):
    def __init__(self) -> None:
        self._handlers: list[EventHandler] = []
        self.history: list[CmsEvent] = []

    def subscribe(self, handler: EventHandler) -> None:
        self._handlers.append(handler)

    async def publish(self, event: CmsEvent) -> None:
        self.history.append(event)
        if self._handlers:
            await asyncio.gather(*(h(event) for h in self._handlers), return_exceptions=True)

    def clear(self) -> None:
        self.history.clear()
