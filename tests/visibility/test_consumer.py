"""Cross-process consumer: unified Event -> ActivityEvent -> recorded."""

from __future__ import annotations

from app.services.nudge.events import InMemoryNudgeEventBus, NudgeEvent, NudgeEventType
from app.services.visibility import ActivitySource, build_visibility_service
from app.services.visibility.consumer import activity_from_event, subscribe_visibility
from app.shared.events import Event, InMemoryEventBus, connect_forwarders


def test_activity_from_event_maps_source_and_refs():
    event = Event(
        type="document.completed",
        source="document",
        document_id="doc_1",
        batch_id="batch_1",
        application_ref="app_1",
        payload={"k": "v"},
    )
    activity = activity_from_event(event)
    assert activity.source == ActivitySource.DOCUMENT
    assert activity.type == "document.completed"
    assert activity.document_id == "doc_1"
    assert activity.batch_id == "batch_1"
    assert activity.application_ref == "app_1"
    assert activity.payload == {"k": "v"}


def test_unknown_source_falls_back_to_system():
    activity = activity_from_event(Event(type="x", source="not-a-service"))
    assert activity.source == ActivitySource.SYSTEM


async def test_consumer_records_published_events():
    bus = InMemoryEventBus()
    service = build_visibility_service()
    subscribe_visibility(bus, service)

    await bus.publish(Event(type="workflow.run.started", source="workflow", run_id="r1"))
    await bus.publish(Event(type="nudge.reminder.sent", source="nudge", application_ref="a1"))

    activities = await service.list_activities()
    assert {a.type for a in activities} == {"workflow.run.started", "nudge.reminder.sent"}
    assert {str(a.source) for a in activities} == {"workflow", "nudge"}


async def test_end_to_end_typed_event_reaches_visibility():
    # Typed nudge bus -> forwarder -> unified bus -> visibility consumer.
    unified = InMemoryEventBus()
    nudge_bus = InMemoryNudgeEventBus()
    service = build_visibility_service()

    connect_forwarders(unified, nudge=nudge_bus)
    subscribe_visibility(unified, service)

    await nudge_bus.publish(
        NudgeEvent(
            type=NudgeEventType.SEQUENCE_ESCALATED,
            sequence_id="seq_1",
            reason="docs_missing",
            target_ref="app_42",
        )
    )

    activities = await service.list_activities()
    assert len(activities) == 1
    recorded = activities[0]
    assert recorded.source == ActivitySource.NUDGE
    assert recorded.type == str(NudgeEventType.SEQUENCE_ESCALATED)
    assert recorded.application_ref == "app_42"
    assert recorded.payload["reason"] == "docs_missing"
