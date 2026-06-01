"""Redis-backed config cache with cross-instance pub/sub invalidation.

This is what makes the <5-minute propagation requirement hold across *multiple*
service instances: an L1 in-process cache (fast) backed by a shared Redis value
store (L2), plus a pub/sub channel so that a write on any instance evicts the L1
copy on every other instance immediately. The short TTL is the safety net if a
pub/sub message is ever missed.

The KV + broadcaster seams are injected so the logic is fully testable without a
running Redis; ``build_redis_config_cache`` wires the real Redis client (lazily).
"""

from __future__ import annotations

import time
from abc import ABC, abstractmethod
from collections.abc import Callable
from dataclasses import dataclass

from .cache import DEFAULT_TTL_SECONDS, ConfigCache
from .models import ConfigRecord

_CLEAR = "*"


class KvStore(ABC):
    """Shared key/value store (Redis strings) with TTL."""

    @abstractmethod
    async def get(self, key: str) -> str | None: ...

    @abstractmethod
    async def set(self, key: str, value: str, ttl: float) -> None: ...

    @abstractmethod
    async def delete(self, key: str) -> None: ...


class Broadcaster(ABC):
    """Pub/sub seam for cross-instance invalidation."""

    @abstractmethod
    async def publish(self, message: str) -> None: ...

    @abstractmethod
    def subscribe(self, handler: Callable[[str], None]) -> None: ...


@dataclass
class _Entry:
    record: ConfigRecord
    expires_at: float


class RedisConfigCache(ConfigCache):
    """L1 (in-process) + L2 (KV) cache with pub/sub L1 eviction."""

    def __init__(
        self,
        *,
        kv: KvStore,
        broadcaster: Broadcaster,
        ttl_seconds: float = DEFAULT_TTL_SECONDS,
        time_fn: Callable[[], float] | None = None,
    ) -> None:
        self._kv = kv
        self._bcast = broadcaster
        self._ttl = ttl_seconds
        self._time = time_fn or time.monotonic
        self._l1: dict[str, _Entry] = {}
        self._bcast.subscribe(self._on_message)

    async def get(self, cache_id: str) -> ConfigRecord | None:
        entry = self._l1.get(cache_id)
        if entry is not None and entry.expires_at > self._time():
            return entry.record
        raw = await self._kv.get(cache_id)
        if raw is None:
            self._l1.pop(cache_id, None)
            return None
        record = ConfigRecord.model_validate_json(raw)
        self._l1[cache_id] = _Entry(record, self._time() + self._ttl)
        return record

    async def set(self, cache_id: str, record: ConfigRecord) -> None:
        self._l1[cache_id] = _Entry(record, self._time() + self._ttl)
        await self._kv.set(cache_id, record.model_dump_json(), self._ttl)

    async def invalidate(self, cache_id: str) -> None:
        self._l1.pop(cache_id, None)
        await self._kv.delete(cache_id)
        await self._bcast.publish(cache_id)  # evict L1 on every other instance

    async def clear(self) -> None:
        self._l1.clear()
        await self._bcast.publish(_CLEAR)

    def _on_message(self, message: str) -> None:
        if message == _CLEAR:
            self._l1.clear()
        else:
            self._l1.pop(message, None)


# -- real Redis wiring (lazy) ------------------------------------------------


def build_redis_config_cache(
    redis_url: str, *, channel: str = "cms:invalidate", ttl_seconds: float = DEFAULT_TTL_SECONDS
) -> RedisConfigCache:
    """Wire a :class:`RedisConfigCache` to a real Redis (KV + pub/sub).

    The pub/sub listener runs as a background task started on first use.
    """

    import asyncio

    import redis.asyncio as aioredis

    client = aioredis.from_url(redis_url, encoding="utf-8", decode_responses=True)

    class _RedisKv(KvStore):
        async def get(self, key: str) -> str | None:
            value = await client.get(key)
            return value if value is None else str(value)

        async def set(self, key: str, value: str, ttl: float) -> None:
            await client.set(key, value, ex=int(ttl))

        async def delete(self, key: str) -> None:
            await client.delete(key)

    class _RedisBroadcaster(Broadcaster):
        def __init__(self) -> None:
            self._handlers: list[Callable[[str], None]] = []
            self._task: asyncio.Task[None] | None = None

        def subscribe(self, handler: Callable[[str], None]) -> None:
            self._handlers.append(handler)
            if self._task is None:
                self._task = asyncio.ensure_future(self._listen())

        async def publish(self, message: str) -> None:
            await client.publish(channel, message)

        async def _listen(self) -> None:  # pragma: no cover - needs a live Redis
            pubsub = client.pubsub()
            await pubsub.subscribe(channel)
            async for event in pubsub.listen():
                if event.get("type") == "message":
                    for handler in self._handlers:
                        handler(event["data"])

    return RedisConfigCache(kv=_RedisKv(), broadcaster=_RedisBroadcaster(), ttl_seconds=ttl_seconds)
