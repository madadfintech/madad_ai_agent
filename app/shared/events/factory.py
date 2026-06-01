"""Construct the unified event bus from settings."""

from __future__ import annotations

from functools import lru_cache

from app.core.config import Settings
from app.core.config import settings as default_settings

from .bus import EventBus, InMemoryEventBus
from .stream import RedisStreamTransport, StreamEventBus


def build_event_bus(settings: Settings | None = None) -> EventBus:
    """Select the in-process bus (dev/tests) or the Redis Streams bus (prod)."""

    settings = settings or default_settings
    transport = settings.events.transport
    if transport == "memory":
        return InMemoryEventBus()
    if transport == "redis":
        return StreamEventBus(
            RedisStreamTransport(settings.events, settings.redis.url),
            settings=settings.events,
        )
    raise ValueError(f"Unknown events.transport: {transport!r}")


@lru_cache(maxsize=1)
def get_event_bus() -> EventBus:
    """Process-singleton unified bus; transport selected by settings."""

    return build_event_bus()
