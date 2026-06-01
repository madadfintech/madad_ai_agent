"""Communication review log, conversation replay, workflow visibility."""

from __future__ import annotations

from app.services.visibility import (
    ActivitySource,
    InMemoryMessageSource,
    ReplayMessage,
    build_visibility_service,
)

from .conftest import activity, at

COMM = ActivitySource.COMMUNICATION
WF = ActivitySource.WORKFLOW


async def test_list_conversations_and_log(service):
    await service.record(
        activity(
            COMM,
            "communication.message.received",
            t=1,
            conversation_id="c1",
            identity="+974",
            channel="whatsapp",
        )
    )
    await service.record(activity(COMM, "communication.message.sent", t=2, conversation_id="c1"))
    await service.record(
        activity(COMM, "communication.message.received", t=1, conversation_id="c2")
    )

    conversations = await service.list_conversations()
    by_id = {c.conversation_id: c for c in conversations}
    assert by_id["c1"].activity_count == 2
    assert by_id["c2"].activity_count == 1

    log = await service.get_conversation_log("c1")
    assert [a.type for a in log] == [
        "communication.message.received",
        "communication.message.sent",
    ]


async def test_replay_merges_message_content():
    source = InMemoryMessageSource()
    source.add(
        "c1",
        ReplayMessage(
            message_id="m1",
            direction="inbound",
            type="text",
            status="received",
            text="YES",
            occurred_at=at(1),
        ),
    )
    source.add(
        "c1",
        ReplayMessage(
            message_id="m2",
            direction="outbound",
            type="template",
            status="sent",
            text="Welcome!",
            occurred_at=at(2),
        ),
    )
    service = build_visibility_service(message_source=source)

    await service.record(
        activity(COMM, "communication.message.received", t=1, conversation_id="c1")
    )
    await service.record(activity(COMM, "communication.message.sent", t=2, conversation_id="c1"))

    replay = await service.replay_conversation("c1")
    assert replay.message_count == 2
    assert [e.summary for e in replay.entries] == ["YES", "Welcome!"]
    assert all(e.kind == "message" for e in replay.entries)


async def test_replay_without_message_source_uses_activities(service):
    await service.record(
        activity(COMM, "communication.message.received", t=1, conversation_id="c9")
    )
    replay = await service.replay_conversation("c9")
    assert replay.message_count == 0
    assert len(replay.entries) == 1
    assert replay.entries[0].kind == "event"


async def test_workflow_timeline_and_summary(service):
    await service.record(
        activity(
            WF, "workflow.run.started", t=1, run_id="r1", workflow="onboarding", session_id="s1"
        )
    )
    await service.record(
        activity(WF, "workflow.step.completed", t=2, run_id="r1", workflow="onboarding")
    )
    await service.record(
        activity(WF, "workflow.run.completed", t=3, run_id="r1", workflow="onboarding")
    )

    timeline = await service.get_workflow_timeline("r1")
    assert len(timeline) == 3

    summary = await service.get_workflow_summary("r1")
    assert summary.workflow == "onboarding"
    assert summary.status == "completed"
    assert summary.event_count == 3
    assert summary.session_id == "s1"

    runs = await service.list_workflow_runs()
    assert len(runs) == 1 and runs[0].run_id == "r1"


async def test_workflow_summary_status_running_when_open(service):
    await service.record(
        activity(WF, "workflow.run.started", t=1, run_id="r2", workflow="onboarding")
    )
    await service.record(activity(WF, "workflow.run.suspended", t=2, run_id="r2"))
    summary = await service.get_workflow_summary("r2")
    assert summary.status == "waiting"
