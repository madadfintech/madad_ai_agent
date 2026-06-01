"""Redis adapters: session store and Streams event bus.

Production implementations of the :class:`SessionStore` and :class:`EventBus`
ports. ``redis.asyncio`` is imported lazily so the rest of the runtime stays
importable without the redis driver installed.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from app.core.config import RedisSettings

from ..events import EventBus, EventHandler, WorkflowEvent
from ..session import Session, SessionStore

if TYPE_CHECKING:  # pragma: no cover
    from redis.asyncio import Redis


def _make_client(url: str) -> Redis:
    import redis.asyncio as aioredis

    return aioredis.from_url(url, encoding="utf-8", decode_responses=True)


class RedisSessionStore(SessionStore):
    """Session store backed by Redis string keys (one JSON blob per session)."""

    def __init__(self, settings: RedisSettings) -> None:
        self._settings = settings
        self._client: Redis | None = None

    @property
    def client(self) -> Redis:
        if self._client is None:
            self._client = _make_client(self._settings.url)
        return self._client

    def _key(self, session_id: str) -> str:
        return f"{self._settings.key_prefix}:session:{session_id}"

    async def get(self, session_id: str) -> Session | None:
        raw = await self.client.get(self._key(session_id))
        return Session.model_validate_json(raw) if raw else None

    async def save(self, session: Session) -> Session:
        await self.client.set(self._key(session.session_id), session.model_dump_json())
        return session

    async def delete(self, session_id: str) -> None:
        await self.client.delete(self._key(session_id))

    async def aclose(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None


class RedisStreamEventBus(EventBus):
    """Event bus that appends workflow events to a Redis Stream.

    Consumers (Nudge, Operational Visibility) read via Redis consumer groups, so
    in-process :meth:`subscribe` is intentionally a no-op here.
    """

    def __init__(self, settings: RedisSettings) -> None:
        self._settings = settings
        self._client: Redis | None = None

    @property
    def client(self) -> Redis:
        if self._client is None:
            self._client = _make_client(self._settings.url)
        return self._client

    def subscribe(self, handler: EventHandler) -> None:  # pragma: no cover
        # Production uses external consumer groups, not in-process handlers.
        return None

    async def publish(self, event: WorkflowEvent) -> None:
        await self.client.xadd(
            self._settings.event_stream,
            {"data": event.model_dump_json()},
            maxlen=self._settings.max_stream_len,
            approximate=True,
        )

    async def aclose(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None
