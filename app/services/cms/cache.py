"""Config cache — the heart of the <5-minute propagation requirement.

Reads are cached with a short TTL; every write invalidates the affected key. In a
single process (tests/dev) ``invalidate`` simply evicts. In production the Redis
adapter publishes the invalidation over pub/sub so every service instance evicts
immediately — giving near-instant propagation, with the short TTL as a safety net
if a pub/sub message is ever missed.
"""

from __future__ import annotations

import time
from abc import ABC, abstractmethod
from collections.abc import Callable
from dataclasses import dataclass

from .models import ConfigRecord

# Default cache TTL. Worst-case propagation if a pub/sub invalidation is missed.
DEFAULT_TTL_SECONDS = 60.0


class ConfigCache(ABC):
    """Port for caching current config records by cache id."""

    @abstractmethod
    async def get(self, cache_id: str) -> ConfigRecord | None: ...

    @abstractmethod
    async def set(self, cache_id: str, record: ConfigRecord) -> None: ...

    @abstractmethod
    async def invalidate(self, cache_id: str) -> None:
        """Evict an entry (and, in production, broadcast the eviction)."""

    @abstractmethod
    async def clear(self) -> None: ...


@dataclass
class _Entry:
    record: ConfigRecord
    expires_at: float


class InMemoryConfigCache(ConfigCache):
    """Process-local TTL cache. ``time_fn`` is injectable for deterministic tests."""

    def __init__(
        self,
        *,
        ttl_seconds: float = DEFAULT_TTL_SECONDS,
        time_fn: Callable[[], float] | None = None,
    ) -> None:
        self._ttl = ttl_seconds
        self._time = time_fn or time.monotonic
        self._entries: dict[str, _Entry] = {}

    async def get(self, cache_id: str) -> ConfigRecord | None:
        entry = self._entries.get(cache_id)
        if entry is None:
            return None
        if entry.expires_at <= self._time():
            self._entries.pop(cache_id, None)
            return None
        return entry.record

    async def set(self, cache_id: str, record: ConfigRecord) -> None:
        self._entries[cache_id] = _Entry(record=record, expires_at=self._time() + self._ttl)

    async def invalidate(self, cache_id: str) -> None:
        self._entries.pop(cache_id, None)

    async def clear(self) -> None:
        self._entries.clear()
