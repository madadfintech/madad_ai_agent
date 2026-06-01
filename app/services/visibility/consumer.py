"""Cross-process consumer: unified ``Event`` -> ``ActivityEvent``.

In production Operational Visibility no longer subscribes to each service's
in-process bus (those run in other processes). Instead it consumes the unified
Redis Stream and maps every :class:`Event` to an :class:`ActivityEvent`. The
in-process ``bridges`` remain for single-process/dev wiring; this is the
multi-process path.
"""

from __future__ import annotations

from datetime import datetime

from app.shared.events import Event, EventBus, StreamEventBus
from app.shared.workflow.utils import utcnow

from .enums import ActivitySource
from .models import ActivityEvent
from .service import OperationalVisibilityService


def _ts(value: str) -> datetime:
    try:
        return datetime.fromisoformat(value)
    except ValueError:  # pragma: no cover - defensive
        return utcnow()


def _source(value: str) -> ActivitySource:
    try:
        return ActivitySource(value)
    except ValueError:  # pragma: no cover - unknown producer
        return ActivitySource.SYSTEM


def activity_from_event(event: Event) -> ActivityEvent:
    """Normalize a unified event into the visibility activity record."""

    return ActivityEvent(
        source=_source(event.source),
        type=event.type,
        occurred_at=_ts(event.occurred_at),
        session_id=event.session_id,
        conversation_id=event.conversation_id,
        run_id=event.run_id,
        document_id=event.document_id,
        batch_id=event.batch_id,
        application_ref=event.application_ref,
        identity=event.identity,
        channel=event.channel,
        workflow=event.workflow,
        summary=event.summary or event.type,
        payload=event.payload,
    )


def subscribe_visibility(bus: EventBus, service: OperationalVisibilityService) -> None:
    """Wire the visibility service to record every event the ``bus`` delivers."""

    async def handle(event: Event) -> None:
        await service.record(activity_from_event(event))

    bus.subscribe(handle)


async def run_visibility_consumer() -> None:  # pragma: no cover - long-running entrypoint
    """Drain the unified stream into the visibility store until cancelled.

    Deployed as its own process: ``python -m app.services.visibility.consumer``.
    Requires ``events.transport=redis`` (the in-process bus has no stream to read).
    """

    from app.shared.events import get_event_bus

    from .deps import get_visibility_service

    bus = get_event_bus()
    if not isinstance(bus, StreamEventBus):
        raise RuntimeError(
            "Visibility consumer requires events.transport=redis; "
            f"got an in-process {type(bus).__name__}."
        )
    subscribe_visibility(bus, get_visibility_service())
    await bus.run_consumer()


def main() -> None:  # pragma: no cover - process entrypoint
    import asyncio

    asyncio.run(run_visibility_consumer())


if __name__ == "__main__":  # pragma: no cover
    main()
