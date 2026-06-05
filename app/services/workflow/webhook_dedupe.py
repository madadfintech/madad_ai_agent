"""Webhook event dedupe — claims an event_id once per (24h) TTL.

Used by the dispatcher to guarantee at-most-once-per-id resumption when
Madad's backend (re)sends a webhook. The in-memory implementation is for
tests and single-process runs; the Redis implementation is the production
path (``SET madad:webhook:event:{event_id} 1 NX EX 86400``).

The contract is just one method: ``claim(event_id)`` returns True if the
caller is the first to claim, False if the id was seen recently.
"""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable


@runtime_checkable
class WebhookDedupe(Protocol):
    async def claim(self, event_id: str, *, ttl_seconds: int = 86_400) -> bool: ...


class InMemoryWebhookDedupe:
    """Dict-based dedupe. TTL is not enforced (tests don't need it); the set
    of seen ids is bounded only by process lifetime."""

    def __init__(self) -> None:
        self.seen: set[str] = set()

    async def claim(self, event_id: str, *, ttl_seconds: int = 86_400) -> bool:
        if event_id in self.seen:
            return False
        self.seen.add(event_id)
        return True


class RedisWebhookDedupe:
    """SET NX EX 86400 over ``{prefix}:webhook:event:{event_id}``.

    Connection is lazy — importing this module does not require Redis to
    be installed. The first ``claim`` call connects; ``aclose`` releases.
    """

    def __init__(self, *, url: str, key_prefix: str = "madad") -> None:
        self._url = url
        self._prefix = key_prefix
        self._client: Any | None = None

    async def _conn(self) -> Any:
        if self._client is None:
            import redis.asyncio as aioredis

            self._client = aioredis.from_url(self._url, decode_responses=True)
        return self._client

    async def claim(self, event_id: str, *, ttl_seconds: int = 86_400) -> bool:
        client = await self._conn()
        key = f"{self._prefix}:webhook:event:{event_id}"
        was_set = await client.set(key, "1", nx=True, ex=ttl_seconds)
        return bool(was_set)

    async def aclose(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None
