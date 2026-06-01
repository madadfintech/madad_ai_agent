"""Sequence scheduling, full-sequence execution, idempotency, multi-channel."""

from __future__ import annotations

from app.services.nudge import NudgeEventType, ReminderStatus, SequenceStatus
from app.shared.workflow.enums import Channel

from .conftest import step

WA = Channel.WHATSAPP
EMAIL = Channel.EMAIL
TARGETS = {WA: "+97455500001"}


async def test_start_schedules_first_reminder_not_yet_due(harness):
    harness.add_schedule("docs", step(10))
    sequence = await harness.service.start_sequence("docs", TARGETS)

    assert sequence.status == SequenceStatus.ACTIVE
    reminders = await harness.service.list_reminders(sequence.sequence_id)
    assert len(reminders) == 1
    assert reminders[0].status == ReminderStatus.SCHEDULED

    # Nothing due at base time (offset is 10s in the future).
    assert await harness.service.run_due(now=harness.clock.now()) == []
    assert harness.dispatcher.sent == []


async def test_full_sequence_runs_to_completion(harness):
    harness.add_schedule("docs", step(10), step(20), step(30))
    sequence = await harness.service.start_sequence("docs", TARGETS)
    base = harness.clock.now()

    from datetime import timedelta

    await harness.service.run_due(now=base + timedelta(seconds=10))
    await harness.service.run_due(now=base + timedelta(seconds=20))
    await harness.service.run_due(now=base + timedelta(seconds=30))

    assert len(harness.dispatcher.sent) == 3
    final = await harness.service.get_sequence(sequence.sequence_id)
    assert final.status == SequenceStatus.COMPLETED
    assert NudgeEventType.SEQUENCE_COMPLETED in harness.event_types()


async def test_idempotent_start_per_target_ref(harness):
    harness.add_schedule("docs", step(10))
    first = await harness.service.start_sequence("docs", TARGETS, target_ref="APP-1")
    second = await harness.service.start_sequence("docs", TARGETS, target_ref="APP-1")

    assert first.sequence_id == second.sequence_id
    # Only one reminder scheduled (no duplicate nudges).
    assert len(await harness.service.list_reminders(first.sequence_id)) == 1


async def test_multichannel_step_dispatches_to_all_available_targets(harness):
    harness.add_schedule("alert", step(0, WA, EMAIL))
    targets = {WA: "+97455500001", EMAIL: "sme@example.com"}
    await harness.service.start_sequence("alert", targets)

    await harness.service.run_due(now=harness.clock.now())
    channels = {s["channel"] for s in harness.dispatcher.sent}
    assert channels == {WA, EMAIL}


async def test_multichannel_skips_missing_target(harness):
    harness.add_schedule("alert", step(0, WA, EMAIL))
    await harness.service.start_sequence("alert", {WA: "+97455500001"})  # no email

    await harness.service.run_due(now=harness.clock.now())
    assert len(harness.dispatcher.sent) == 1
    assert harness.dispatcher.sent[0]["channel"] == WA
