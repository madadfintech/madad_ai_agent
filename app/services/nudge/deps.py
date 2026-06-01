"""Dependency wiring for the nudge service."""

from __future__ import annotations

from functools import lru_cache

from app.shared.workflow.context import Clock

from .audit import NudgeAuditLogger
from .config_provider import InMemoryNudgeConfigProvider, NudgeConfigProvider
from .dispatcher import InMemoryNotificationDispatcher, NotificationDispatcher
from .events import InMemoryNudgeEventBus, NudgeEventBus
from .persistence import InMemoryNudgeStore, NudgeStore
from .service import NudgeConfig, NudgeService


def build_nudge_service(
    *,
    store: NudgeStore | None = None,
    config_provider: NudgeConfigProvider | None = None,
    dispatcher: NotificationDispatcher | None = None,
    events: NudgeEventBus | None = None,
    config: NudgeConfig | None = None,
    clock: Clock | None = None,
) -> NudgeService:
    return NudgeService(
        store=store or InMemoryNudgeStore(),
        config_provider=config_provider or InMemoryNudgeConfigProvider(),
        dispatcher=dispatcher or InMemoryNotificationDispatcher(),
        events=events or InMemoryNudgeEventBus(),
        audit=NudgeAuditLogger(),
        config=config,
        clock=clock,
    )


@lru_cache(maxsize=1)
def get_nudge_service() -> NudgeService:
    """Process-singleton service; backend selected by settings."""

    from app.core.config import settings

    if settings.persistence.backend == "postgres":
        from app.shared.db.provider import get_database

        from .db import PostgresNudgeStore

        return build_nudge_service(store=PostgresNudgeStore(get_database()))
    return build_nudge_service()
