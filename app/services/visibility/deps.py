"""Dependency wiring for the Operational Visibility service."""

from __future__ import annotations

from functools import lru_cache

from .persistence import ActivityStore, InMemoryActivityStore
from .projections import FunnelConfig
from .service import OperationalVisibilityService
from .sources import MessageSource


def build_visibility_service(
    *,
    store: ActivityStore | None = None,
    message_source: MessageSource | None = None,
    funnel_config: FunnelConfig | None = None,
) -> OperationalVisibilityService:
    return OperationalVisibilityService(
        store=store or InMemoryActivityStore(),
        message_source=message_source,
        funnel_config=funnel_config,
    )


@lru_cache(maxsize=1)
def get_visibility_service() -> OperationalVisibilityService:
    """Process-singleton service; backend selected by settings."""

    from app.core.config import settings

    if settings.persistence.backend == "postgres":
        from app.shared.db.provider import get_database

        from .db import PostgresActivityStore

        return build_visibility_service(store=PostgresActivityStore(get_database()))
    return build_visibility_service()
