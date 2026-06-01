"""Event bridges — normalize each service's events into ``ActivityEvent``.

These map the source services' event types onto the common activity record and
wire in-process subscriptions. Operational Visibility is the observer at the top
of the dependency graph, so importing the observed services here is correct.

In production, a Redis Streams / OpenSearch consumer replaces these in-process
subscriptions; the mappers stay the same.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from datetime import datetime
from typing import Protocol

from app.services.cms.events import CmsEvent, CmsEventBus
from app.services.communication.events import CommunicationEvent, CommunicationEventBus
from app.services.document.events import DocumentEvent, DocumentEventBus
from app.services.nudge.events import NudgeEvent, NudgeEventBus
from app.shared.workflow.events import EventBus as WorkflowEventBus
from app.shared.workflow.events import WorkflowEvent

from .enums import ActivitySource
from .models import ActivityEvent


class SupportsRecord(Protocol):
    async def record(self, activity: ActivityEvent) -> ActivityEvent: ...


def _ts(value: str) -> datetime:
    try:
        return datetime.fromisoformat(value)
    except ValueError:  # pragma: no cover - defensive
        from app.shared.workflow.utils import utcnow

        return utcnow()


def _channel(value: object) -> str | None:
    return str(value) if value is not None else None


def workflow_to_activity(event: WorkflowEvent) -> ActivityEvent:
    return ActivityEvent(
        source=ActivitySource.WORKFLOW,
        type=str(event.type),
        occurred_at=_ts(event.occurred_at),
        run_id=event.run_id,
        session_id=event.session_id,
        workflow=event.workflow,
        channel=_channel(event.channel),
        identity=event.identity or None,
        summary=f"{event.workflow}: {event.type}",
        payload=event.payload,
    )


def communication_to_activity(event: CommunicationEvent) -> ActivityEvent:
    return ActivityEvent(
        source=ActivitySource.COMMUNICATION,
        type=str(event.type),
        occurred_at=_ts(event.occurred_at),
        conversation_id=event.conversation_id,
        channel=_channel(event.channel),
        identity=event.identity or None,
        summary=str(event.type),
        payload={"message_id": event.message_id, **event.payload},
    )


def nudge_to_activity(event: NudgeEvent) -> ActivityEvent:
    return ActivityEvent(
        source=ActivitySource.NUDGE,
        type=str(event.type),
        occurred_at=_ts(event.occurred_at),
        application_ref=event.target_ref,
        summary=f"{event.reason}: {event.type}" if event.reason else str(event.type),
        payload={
            "sequence_id": event.sequence_id,
            "reminder_id": event.reminder_id,
            "reason": event.reason,
            **event.payload,
        },
    )


def document_to_activity(event: DocumentEvent) -> ActivityEvent:
    return ActivityEvent(
        source=ActivitySource.DOCUMENT,
        type=str(event.type),
        occurred_at=_ts(event.occurred_at),
        document_id=event.document_id,
        batch_id=event.batch_id,
        application_ref=event.application_ref,
        summary=str(event.type),
        payload=event.payload,
    )


def cms_to_activity(event: CmsEvent) -> ActivityEvent:
    return ActivityEvent(
        source=ActivitySource.CMS,
        type=str(event.type),
        occurred_at=_ts(event.occurred_at),
        summary=f"{event.kind}:{event.name}" if event.name else str(event.type),
        payload={
            "kind": str(event.kind) if event.kind else None,
            "name": event.name,
            "version": event.version,
        },
    )


def _handler(
    mapper: Callable[..., ActivityEvent], recorder: SupportsRecord
) -> Callable[..., Awaitable[None]]:
    async def handle(event: object) -> None:
        await recorder.record(mapper(event))

    return handle


def subscribe_workflow(bus: WorkflowEventBus, recorder: SupportsRecord) -> None:
    bus.subscribe(_handler(workflow_to_activity, recorder))


def subscribe_communication(bus: CommunicationEventBus, recorder: SupportsRecord) -> None:
    bus.subscribe(_handler(communication_to_activity, recorder))


def subscribe_nudge(bus: NudgeEventBus, recorder: SupportsRecord) -> None:
    bus.subscribe(_handler(nudge_to_activity, recorder))


def subscribe_document(bus: DocumentEventBus, recorder: SupportsRecord) -> None:
    bus.subscribe(_handler(document_to_activity, recorder))


def subscribe_cms(bus: CmsEventBus, recorder: SupportsRecord) -> None:
    bus.subscribe(_handler(cms_to_activity, recorder))
