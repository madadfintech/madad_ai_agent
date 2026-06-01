"""Real-Redis coverage for the unified bus's Redis Streams transport.

Skipped unless ``REDIS_URL`` is set (the CI integration job sets it). Exercises
RedisStreamTransport end-to-end: XADD publish, consumer-group create, XREADGROUP,
dispatch, and XACK — the path the in-memory transport can only approximate.
"""

from __future__ import annotations

import os
import uuid
from collections.abc import Awaitable, Callable

import pytest

from app.core.config import EventBusSettings
from app.shared.events import Event, StreamEventBus
from app.shared.events.stream import RedisStreamTransport

REDIS_URL = os.getenv("REDIS_URL")
pytestmark = pytest.mark.skipif(not REDIS_URL, reason="requires REDIS_URL")


def _collector(sink: list[Event]) -> Callable[[Event], Awaitable[None]]:
    async def handle(event: Event) -> None:
        sink.append(event)

    return handle


async def test_publish_consume_ack_roundtrip():
    # Unique stream/group per run so repeated CI runs don't collide.
    token = uuid.uuid4().hex[:8]
    settings = EventBusSettings(
        transport="redis",
        stream=f"stream:test:{token}",
        group=f"g-{token}",
        consumer="c1",
    )
    transport = RedisStreamTransport(settings, REDIS_URL)  # type: ignore[arg-type]
    bus = StreamEventBus(transport, settings=settings)
    seen: list[Event] = []
    bus.subscribe(_collector(seen))

    try:
        await bus.publish(Event(type="workflow.run.started", source="workflow", run_id="r1"))
        await bus.publish(Event(type="nudge.reminder.sent", source="nudge", application_ref="a1"))

        drained = await bus.consume_once(count=10, block_ms=500)
        assert drained == 2
        assert {e.run_id for e in seen} == {"r1", None}
        assert {e.source for e in seen} == {"workflow", "nudge"}

        # Everything was acked: nothing redelivered on the next read.
        assert await bus.consume_once(count=10, block_ms=200) == 0
    finally:
        await transport.client.delete(settings.stream)
        await transport.aclose()
