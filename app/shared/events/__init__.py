"""Unified cross-process event bus.

A single normalized :class:`Event` envelope plus an :class:`EventBus` port with an
in-process default and a Redis Streams transport. Per-service typed buses remain
the domain transport; :func:`connect_forwarders` republishes their events onto
this unified bus, which the Operational Visibility worker consumes cross-process.
"""

from __future__ import annotations

from .bus import EventBus, EventHandler, InMemoryEventBus
from .event import Event
from .factory import build_event_bus, get_event_bus
from .forward import connect_forwarders, forward_to, to_event
from .stream import (
    InMemoryStreamTransport,
    RedisStreamTransport,
    StreamEventBus,
    StreamTransport,
)

__all__ = [
    "Event",
    "EventBus",
    "EventHandler",
    "InMemoryEventBus",
    "InMemoryStreamTransport",
    "RedisStreamTransport",
    "StreamEventBus",
    "StreamTransport",
    "build_event_bus",
    "connect_forwarders",
    "forward_to",
    "get_event_bus",
    "to_event",
]
