"""Forward per-service typed events onto the unified bus.

Each service keeps its own in-process typed bus (the domain transport). To make
events cross process boundaries, a forwarder subscribes to a typed bus, maps each
typed event to the unified :class:`Event`, and republishes it on the unified bus
(Redis Streams in production). This is the additive "unify the buses" seam: the
typed buses are untouched; the unified bus is the single cross-process stream.

The mapping is generic — it reads the envelope refs the unified event models and
carries every other field into ``payload`` — so it works for all five typed
event shapes without per-service code.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

from app.shared.workflow.utils import utcnow

from .bus import EventBus
from .event import Event

# Fields the unified envelope models directly; everything else goes to payload.
# ``target_ref`` (nudge) maps onto ``application_ref`` and is excluded here so it
# is not also duplicated into payload.
_ENVELOPE_FIELDS = frozenset(
    {
        "event_id",
        "type",
        "occurred_at",
        "session_id",
        "conversation_id",
        "run_id",
        "document_id",
        "batch_id",
        "application_ref",
        "target_ref",
        "identity",
        "channel",
        "workflow",
        "correlation_id",
        "summary",
        "payload",
    }
)


def to_event(typed: Any, source: str) -> Event:
    """Map any service's typed event to the unified :class:`Event`."""

    data: dict[str, Any] = typed.model_dump(mode="json")
    payload = dict(data.get("payload") or {})
    for key, value in data.items():
        if key not in _ENVELOPE_FIELDS and value is not None:
            payload.setdefault(key, value)

    return Event(
        type=str(data["type"]),
        source=source,
        occurred_at=data.get("occurred_at") or utcnow().isoformat(),
        session_id=data.get("session_id"),
        conversation_id=data.get("conversation_id"),
        run_id=data.get("run_id"),
        document_id=data.get("document_id"),
        batch_id=data.get("batch_id"),
        application_ref=data.get("application_ref") or data.get("target_ref"),
        identity=data.get("identity") or None,
        channel=data.get("channel"),
        workflow=data.get("workflow"),
        correlation_id=data.get("correlation_id"),
        payload=payload,
    )


def forward_to(bus: EventBus, source: str) -> Callable[[Any], Awaitable[None]]:
    """Build a handler that republishes a typed event on the unified ``bus``."""

    async def handler(typed: Any) -> None:
        await bus.publish(to_event(typed, source))

    return handler


def connect_forwarders(
    unified: EventBus,
    *,
    workflow: Any | None = None,
    communication: Any | None = None,
    nudge: Any | None = None,
    document: Any | None = None,
    cms: Any | None = None,
) -> None:
    """Subscribe forwarders onto each provided typed bus.

    Call once at process startup with the in-process buses that exist in this
    process; missing buses are simply skipped.
    """

    for bus, source in (
        (workflow, "workflow"),
        (communication, "communication"),
        (nudge, "nudge"),
        (document, "document"),
        (cms, "cms"),
    ):
        if bus is not None:
            bus.subscribe(forward_to(unified, source))
