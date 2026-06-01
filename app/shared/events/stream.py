"""Cross-process event bus over a stream transport.

:class:`StreamEventBus` is an :class:`EventBus` whose producer side appends to a
durable stream and whose consumer side reads via a consumer group, dispatches to
in-process handlers, and acknowledges. It is parameterised by a
:class:`StreamTransport` so the same bus is exercised in tests against
:class:`InMemoryStreamTransport` and runs in production against
:class:`RedisStreamTransport` (Redis Streams).

Producers (each service process) only call :meth:`publish`. The Operational
Visibility worker process registers handlers and runs :meth:`run_consumer`.
"""

from __future__ import annotations

import asyncio
from abc import ABC, abstractmethod
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING

from app.core.config import EventBusSettings
from app.core.logging import get_logger

from .bus import EventBus, EventHandler
from .event import Event

if TYPE_CHECKING:  # pragma: no cover
    from redis.asyncio import Redis

# A read returns (stream message id, event) pairs to be acked after handling.
StreamRecord = tuple[str, Event]


class StreamTransport(ABC):
    """Raw stream operations the :class:`StreamEventBus` is built on."""

    @abstractmethod
    async def publish(self, event: Event) -> None: ...

    @abstractmethod
    async def ensure_group(self) -> None:
        """Create the consumer group if it does not exist (idempotent)."""

    @abstractmethod
    async def read(self, *, count: int, block_ms: int) -> list[StreamRecord]:
        """Read up to ``count`` new messages for this consumer (blocking)."""

    @abstractmethod
    async def ack(self, message_id: str) -> None: ...

    async def aclose(self) -> None:  # pragma: no cover - default no-op
        return None


class InMemoryStreamTransport(StreamTransport):
    """In-process fake stream with consumer-group + ack semantics for tests."""

    def __init__(self) -> None:
        self._log: list[StreamRecord] = []
        self._seq = 0
        self._cursor = 0  # next undelivered index
        self.pending: dict[str, Event] = {}  # delivered, not yet acked

    async def publish(self, event: Event) -> None:
        self._seq += 1
        self._log.append((str(self._seq), event))

    async def ensure_group(self) -> None:
        return None

    async def read(self, *, count: int, block_ms: int) -> list[StreamRecord]:
        batch = self._log[self._cursor : self._cursor + count]
        self._cursor += len(batch)
        for message_id, event in batch:
            self.pending[message_id] = event
        return batch

    async def ack(self, message_id: str) -> None:
        self.pending.pop(message_id, None)


class RedisStreamTransport(StreamTransport):
    """Redis Streams transport (XADD / XREADGROUP / XACK).

    ``redis.asyncio`` is imported lazily so importing this module never requires
    the driver. The consumer group is created lazily and ``BUSYGROUP`` (already
    exists) is treated as success.
    """

    def __init__(self, settings: EventBusSettings, url: str) -> None:
        self._settings = settings
        self._url = url
        self._client: Redis | None = None
        self._group_ready = False

    @property
    def client(self) -> Redis:
        if self._client is None:
            import redis.asyncio as aioredis

            self._client = aioredis.from_url(
                self._url, encoding="utf-8", decode_responses=True
            )
        return self._client

    async def publish(self, event: Event) -> None:
        await self.client.xadd(
            self._settings.stream,
            {"data": event.model_dump_json()},
            maxlen=self._settings.max_len,
            approximate=True,
        )

    async def ensure_group(self) -> None:
        if self._group_ready:
            return
        try:
            await self.client.xgroup_create(
                self._settings.stream, self._settings.group, id="0", mkstream=True
            )
        except Exception as exc:  # noqa: BLE001 - BUSYGROUP means it already exists
            if "BUSYGROUP" not in str(exc):
                raise
        self._group_ready = True

    async def read(self, *, count: int, block_ms: int) -> list[StreamRecord]:
        response = await self.client.xreadgroup(
            self._settings.group,
            self._settings.consumer,
            {self._settings.stream: ">"},
            count=count,
            block=block_ms,
        )
        if not response:
            return []
        records: list[StreamRecord] = []
        for _stream, messages in response:
            for message_id, fields in messages:
                records.append((message_id, Event.model_validate_json(fields["data"])))
        return records

    async def ack(self, message_id: str) -> None:
        await self.client.xack(self._settings.stream, self._settings.group, message_id)

    async def aclose(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None


StopFn = Callable[[], Awaitable[bool] | bool]


class StreamEventBus(EventBus):
    """Cross-process bus: publish to a stream, consume via a consumer group."""

    def __init__(self, transport: StreamTransport, *, settings: EventBusSettings) -> None:
        self._transport = transport
        self._settings = settings
        self._handlers: list[EventHandler] = []
        self._log = get_logger("events.stream")

    def subscribe(self, handler: EventHandler) -> None:
        self._handlers.append(handler)

    async def publish(self, event: Event) -> None:
        await self._transport.publish(event)

    async def consume_once(
        self, *, count: int | None = None, block_ms: int | None = None
    ) -> int:
        """Read one batch, dispatch each event to handlers, ack. Returns count."""

        await self._transport.ensure_group()
        records = await self._transport.read(
            count=count or self._settings.batch_size,
            block_ms=block_ms if block_ms is not None else self._settings.block_ms,
        )
        for message_id, event in records:
            await self._dispatch(event)
            await self._transport.ack(message_id)
        if records:
            self._log.info("events.consumed", count=len(records))
        return len(records)

    async def run_consumer(self, *, stop: StopFn | None = None) -> None:
        """Drain the stream until ``stop()`` returns true (or forever)."""

        while True:
            await self.consume_once()
            if stop is not None:
                done = stop()
                if (await done) if asyncio.iscoroutine(done) else done:
                    return

    async def _dispatch(self, event: Event) -> None:
        if not self._handlers:
            return
        results = await asyncio.gather(
            *(h(event) for h in self._handlers), return_exceptions=True
        )
        for result in results:
            if isinstance(result, Exception):
                self._log.warning(
                    "events.handler_failed", event_type=event.type, error=str(result)
                )

    async def aclose(self) -> None:
        await self._transport.aclose()
