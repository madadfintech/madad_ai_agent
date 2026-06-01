"""Service lifespans boot cleanly and wire the unified-bus forwarders."""

from __future__ import annotations

from fastapi.testclient import TestClient

from app.services.cms.main import app as cms_app
from app.services.communication.main import app as comm_app
from app.services.document.main import app as doc_app
from app.services.nudge.deps import get_nudge_service
from app.services.nudge.events import NudgeEvent, NudgeEventType
from app.services.nudge.main import app as nudge_app
from app.services.nudge.main import lifespan as nudge_lifespan
from app.services.workflow.main import app as wf_app
from app.shared.events import InMemoryEventBus, get_event_bus


def test_all_producer_apps_boot_with_lifespan():
    # Entering the context runs each app's startup (forwarder wiring, runtime
    # setup) and shutdown — a regression guard that the lifespans don't crash.
    for app in (comm_app, cms_app, doc_app, nudge_app, wf_app):
        with TestClient(app) as client:
            assert client.get("/health").status_code == 200
            assert client.get("/ready").status_code == 200


async def test_nudge_lifespan_forwards_events_to_unified_bus():
    bus = get_event_bus()
    assert isinstance(bus, InMemoryEventBus)
    before = len(bus.history)

    async with nudge_lifespan(nudge_app):
        await get_nudge_service().events.publish(
            NudgeEvent(
                type=NudgeEventType.SEQUENCE_STARTED,
                sequence_id="seq_lifespan_probe",
                reason="docs",
            )
        )

    forwarded = bus.history[before:]
    assert any(
        e.source == "nudge" and e.payload.get("sequence_id") == "seq_lifespan_probe"
        for e in forwarded
    )
