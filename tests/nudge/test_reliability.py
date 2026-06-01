"""Retry, suppression, escalation."""

from __future__ import annotations

from app.services.nudge import NudgeEventType, ReminderStatus, SequenceStatus
from app.shared.workflow.enums import Channel

from .conftest import step

WA = Channel.WHATSAPP
TARGETS = {WA: "+97455500001"}


async def test_transient_failure_retries_then_sends(make_harness):
    harness = make_harness(fail_times=2)
    harness.add_schedule("docs", step(0), max_attempts=3)
    seq = await harness.service.start_sequence("docs", TARGETS)

    # 1st tick: fails -> RETRYING, rescheduled +1s.
    await harness.service.run_due()
    reminders = await harness.service.list_reminders(seq.sequence_id)
    assert reminders[0].status == ReminderStatus.RETRYING
    assert reminders[0].attempts == 1

    # 2nd attempt after backoff: fails again.
    harness.clock.advance(1)
    await harness.service.run_due()
    # 3rd attempt: succeeds.
    harness.clock.advance(2)
    await harness.service.run_due()

    assert len(harness.dispatcher.sent) == 1
    reminders = await harness.service.list_reminders(seq.sequence_id)
    assert reminders[0].status == ReminderStatus.SENT
    assert NudgeEventType.REMINDER_RETRYING in harness.event_types()


async def test_retry_exhaustion_fails_step_but_advances_sequence(make_harness):
    harness = make_harness(fail_times=100)  # always fails
    harness.add_schedule("docs", step(0, template="s0"), step(60, template="s1"), max_attempts=2)
    seq = await harness.service.start_sequence("docs", TARGETS)

    await harness.service.run_due()  # attempt 1 -> RETRYING
    harness.clock.advance(1)
    await harness.service.run_due()  # attempt 2 == max -> FAILED, schedule next step

    reminders = await harness.service.list_reminders(seq.sequence_id)
    statuses = {r.step_index: r.status for r in reminders}
    assert statuses[0] == ReminderStatus.FAILED
    assert statuses[1] == ReminderStatus.SCHEDULED  # sequence continued
    assert NudgeEventType.REMINDER_FAILED in harness.event_types()


async def test_suppression_cancels_pending_reminder(harness):
    harness.add_schedule("docs", step(10))
    seq = await harness.service.start_sequence("docs", TARGETS)

    await harness.service.suppress(seq.sequence_id)
    suppressed = await harness.service.get_sequence(seq.sequence_id)
    assert suppressed.status == SequenceStatus.SUPPRESSED

    reminders = await harness.service.list_reminders(seq.sequence_id)
    assert reminders[0].status == ReminderStatus.SUPPRESSED

    from datetime import timedelta

    assert await harness.service.run_due(now=harness.clock.now() + timedelta(seconds=60)) == []
    assert harness.dispatcher.sent == []


async def test_suppress_matching_by_target_ref(harness):
    harness.add_schedule("docs", step(10))
    await harness.service.start_sequence("docs", TARGETS, target_ref="APP-9")

    suppressed = await harness.service.suppress_matching(target_ref="APP-9")
    assert len(suppressed) == 1
    assert suppressed[0].status == SequenceStatus.SUPPRESSED


async def test_escalation_step_escalates_sequence(harness):
    harness.add_schedule("overdue", step(0, WA, escalate=True))
    seq = await harness.service.start_sequence("overdue", TARGETS)

    await harness.service.run_due()
    escalated = await harness.service.get_sequence(seq.sequence_id)
    assert escalated.status == SequenceStatus.ESCALATED
    assert len(harness.dispatcher.sent) == 1
    assert NudgeEventType.SEQUENCE_ESCALATED in harness.event_types()
