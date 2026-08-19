"""Human takeover flags — pause the bot while a Madad teammate runs the chat.

When an admin takes over a conversation from the CMS, the bot must go fully
silent for that (channel, identity): inbound user messages are logged by the
backend bridge but NOT dispatched to the workflow, and backend events
(document verified, status changed, ...) update state upstream but produce no
bot messages. When the admin marks the chat resolved, the flag is cleared and
the workflow resumes from whatever the CURRENT state is.

Storage mirrors the webhook-dedupe layer exactly: an in-memory map for tests
and single-process runs, Redis (same connection settings) in production. Keys
carry NO TTL — a takeover lasts until explicitly released. Values record who
took over and when, for the CMS to display.

This module is deliberately self-contained and additive: nothing else in the
workflow imports differently, and with no flag set every path behaves exactly
as before.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any, Protocol


class TakeoverStore(Protocol):
    async def start(self, key: str, *, by: str | None = None) -> dict[str, Any]: ...

    async def end(self, key: str) -> bool: ...

    async def get(self, key: str) -> dict[str, Any] | None: ...


def takeover_key(channel: Any, identity: str) -> str:
    """Stable per-conversation key, matching the session-identity convention."""
    return f"{str(getattr(channel, 'value', channel)).lower()}:{identity.strip().lower()}"


def _record(by: str | None) -> dict[str, Any]:
    return {
        "active": True,
        "by": by,
        "since": datetime.now(timezone.utc).isoformat(),
    }


class InMemoryTakeoverStore:
    """Test / single-process implementation."""

    def __init__(self) -> None:
        self._flags: dict[str, dict[str, Any]] = {}

    async def start(self, key: str, *, by: str | None = None) -> dict[str, Any]:
        rec = self._flags.get(key) or _record(by)
        self._flags[key] = rec
        return rec

    async def end(self, key: str) -> bool:
        return self._flags.pop(key, None) is not None

    async def get(self, key: str) -> dict[str, Any] | None:
        return self._flags.get(key)


_store: TakeoverStore | None = None


def get_takeover_store() -> TakeoverStore:
    """Process-singleton store: Redis when configured (production), else
    in-memory. Self-contained on purpose — nothing else in the platform
    needs rewiring for the takeover feature to exist."""
    global _store
    if _store is None:
        from app.core.config import settings

        _store = (
            RedisTakeoverStore(
                url=settings.redis.url,
                key_prefix=settings.redis.key_prefix,
            )
            if settings.redis.url
            else InMemoryTakeoverStore()
        )
    return _store


class RedisTakeoverStore:
    """Production implementation — same lazy-connection pattern as
    :class:`RedisWebhookDedupe`, so importing never requires Redis."""

    def __init__(self, *, url: str, key_prefix: str = "madad") -> None:
        self._url = url
        self._prefix = f"{key_prefix}:takeover:"
        self._client: Any = None

    async def _conn(self) -> Any:
        if self._client is None:
            import redis.asyncio as aioredis

            self._client = aioredis.from_url(self._url, decode_responses=True)
        return self._client

    async def start(self, key: str, *, by: str | None = None) -> dict[str, Any]:
        conn = await self._conn()
        full = self._prefix + key
        existing = await conn.get(full)
        if existing:
            return json.loads(existing)
        rec = _record(by)
        # No TTL: a takeover lasts until explicitly released.
        await conn.set(full, json.dumps(rec))
        return rec

    async def end(self, key: str) -> bool:
        conn = await self._conn()
        return bool(await conn.delete(self._prefix + key))

    async def get(self, key: str) -> dict[str, Any] | None:
        conn = await self._conn()
        raw = await conn.get(self._prefix + key)
        return json.loads(raw) if raw else None
