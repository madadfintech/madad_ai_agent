"""Unified event bus: in-process fanout, stream consume/ack, forwarders."""

from __future__ import annotations

from collections.abc import Awaitable, Callable

from app.core.config import EventBusSettings
from app.services.cms.enums import ConfigKind
from app.services.cms.events import CmsEvent, CmsEventType, InMemoryCmsEventBus
from app.services.nudge.events import InMemoryNudgeEventBus, NudgeEvent, NudgeEventType
from app.shared.events import (
    Event,
    InMemoryEventBus,
    InMemoryStreamTransport,
    StreamEventBus,
    connect_forwarders,
    to_event,
)
from app.shared.workflow.enums import Channel, WorkflowEventType
from app.shared.workflow.events import WorkflowEvent


def _evt(**kw) -> Event:
    return Event(type="t.test", source="workflow", **kw)


def _collector(sink: list[Event]) -> Callable[[Event], Awaitable[None]]:
    async def handle(event: Event) -> None:
        sink.append(event)

    return handle


async def test_in_memory_fanout_and_history():
    bus = InMemoryEventBus()
    seen: list[Event] = []
    bus.subscribe(_collector(seen))

    await bus.publish(_evt(run_id="r1"))
    await bus.publish(_evt(run_id="r2"))

    assert [e.run_id for e in seen] == ["r1", "r2"]
    assert len(bus.history) == 2


async def test_subscriber_failure_is_isolated():
    bus = InMemoryEventBus()
    seen: list[Event] = []

    async def boom(_: Event) -> None:
        raise RuntimeError("bad subscriber")

    bus.subscribe(boom)
    bus.subscribe(_collector(seen))

    await bus.publish(_evt())  # must not raise
    assert len(seen) == 1


async def test_to_event_maps_workflow_refs():
    src = WorkflowEvent(
        type=WorkflowEventType.RUN_STARTED,
        run_id="run_1",
        session_id="sess_1",
        workflow="onboarding",
        channel=Channel.WHATSAPP,
        identity="+97455500000",
        payload={"step": "welcome"},
    )
    event = to_event(src, "workflow")
    assert event.source == "workflow"
    assert event.type == str(WorkflowEventType.RUN_STARTED)
    assert event.run_id == "run_1"
    assert event.session_id == "sess_1"
    assert event.workflow == "onboarding"
    assert event.channel == str(Channel.WHATSAPP)
    assert event.identity == "+97455500000"
    assert event.payload["step"] == "welcome"


async def test_to_event_carries_source_specific_fields_to_payload():
    # Nudge: target_ref -> application_ref; sequence_id/reason kept in payload.
    nudge = NudgeEvent(
        type=NudgeEventType.REMINDER_SENT,
        sequence_id="seq_1",
        reminder_id="rem_1",
        reason="docs_missing",
        target_ref="app_123",
    )
    event = to_event(nudge, "nudge")
    assert event.application_ref == "app_123"
    assert event.payload["sequence_id"] == "seq_1"
    assert event.payload["reminder_id"] == "rem_1"
    assert event.payload["reason"] == "docs_missing"
    assert "target_ref" not in event.payload  # mapped, not duplicated

    # CMS: kind/name/version carried in payload.
    cms = CmsEvent(
        type=CmsEventType.CONFIG_UPDATED,
        kind=ConfigKind.NUDGE,
        name="docs_missing",
        version=3,
    )
    cms_event = to_event(cms, "cms")
    assert cms_event.payload["name"] == "docs_missing"
    assert cms_event.payload["version"] == 3
    assert cms_event.payload["kind"] == str(ConfigKind.NUDGE)


async def test_empty_identity_normalized_to_none():
    # Workflow/communication default identity to "" — should map to None.
    src = WorkflowEvent(
        type=WorkflowEventType.RUN_STARTED,
        run_id="r",
        session_id="s",
        workflow="onboarding",
    )
    assert to_event(src, "workflow").identity is None


async def test_connect_forwarders_republishes_onto_unified_bus():
    unified = InMemoryEventBus()
    nudge_bus = InMemoryNudgeEventBus()
    cms_bus = InMemoryCmsEventBus()
    connect_forwarders(unified, nudge=nudge_bus, cms=cms_bus)

    await nudge_bus.publish(
        NudgeEvent(type=NudgeEventType.SEQUENCE_STARTED, sequence_id="s1", reason="r")
    )
    await cms_bus.publish(CmsEvent(type=CmsEventType.CONFIG_UPDATED, name="x", version=1))

    assert {e.source for e in unified.history} == {"nudge", "cms"}
    assert {e.type for e in unified.history} == {
        str(NudgeEventType.SEQUENCE_STARTED),
        str(CmsEventType.CONFIG_UPDATED),
    }


async def test_stream_bus_consume_dispatches_and_acks():
    transport = InMemoryStreamTransport()
    bus = StreamEventBus(transport, settings=EventBusSettings())
    seen: list[Event] = []
    bus.subscribe(_collector(seen))

    await bus.publish(_evt(run_id="a"))
    await bus.publish(_evt(run_id="b"))

    drained = await bus.consume_once(count=10, block_ms=0)
    assert drained == 2
    assert [e.run_id for e in seen] == ["a", "b"]
    assert transport.pending == {}  # everything acked

    assert await bus.consume_once(count=10, block_ms=0) == 0  # nothing left


async def test_stream_bus_run_consumer_stops_when_drained():
    transport = InMemoryStreamTransport()
    bus = StreamEventBus(transport, settings=EventBusSettings(batch_size=1))
    seen: list[Event] = []
    bus.subscribe(_collector(seen))
    for i in range(3):
        await bus.publish(_evt(run_id=str(i)))

    await bus.run_consumer(stop=lambda: len(seen) >= 3)
    assert len(seen) == 3
