"""Dependency wiring for the CMS service."""

from __future__ import annotations

from functools import lru_cache

from app.shared.i18n import DEFAULT_LOCALE, Locale

from .audit import CmsAuditLogger
from .cache import ConfigCache, InMemoryConfigCache
from .events import CmsEventBus, InMemoryCmsEventBus
from .persistence import ConfigStore, InMemoryConfigStore
from .service import CmsService


def build_cms_service(
    *,
    store: ConfigStore | None = None,
    cache: ConfigCache | None = None,
    events: CmsEventBus | None = None,
    default_locale: Locale = DEFAULT_LOCALE,
) -> CmsService:
    return CmsService(
        store=store or InMemoryConfigStore(),
        cache=cache or InMemoryConfigCache(),
        events=events or InMemoryCmsEventBus(),
        audit=CmsAuditLogger(),
        default_locale=default_locale,
    )


@lru_cache(maxsize=1)
def get_cms_service() -> CmsService:
    """Process-singleton CMS service; backend selected by settings."""

    from app.core.config import settings

    store: ConfigStore | None = None
    cache: ConfigCache | None = None
    if settings.persistence.backend == "postgres":
        from app.shared.db.provider import get_database

        from .db import PostgresConfigStore

        store = PostgresConfigStore(get_database())
    if settings.persistence.cms_cache == "redis":
        from .cache_redis import build_redis_config_cache

        cache = build_redis_config_cache(settings.redis.url)
    return build_cms_service(store=store, cache=cache)
