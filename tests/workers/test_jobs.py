"""Periodic job functions drive the right service work and return counts."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from app.services.nudge import (
    InMemoryNudgeConfigProvider,
    NudgeScheduleConfig,
    NudgeStep,
    build_nudge_service,
)
from app.services.workflow.deps import build_onboarding_platform
from app.shared.workflow.context import Clock
from app.shared.workflow.enums import Channel
from app.workers import jobs


class FixedClock(Clock):
    def __init__(self) -> None:
        self._now = datetime(2026, 5, 21, 12, 0, 0, tzinfo=UTC)

    def now(self) -> datetime:
        return self._now

    def advance(self, seconds: float) -> None:
        self._now += timedelta(seconds=seconds)


async def test_run_due_nudges_dispatches_due_reminders():
    clock = FixedClock()
    provider = InMemoryNudgeConfigProvider()
    provider.add(
        NudgeScheduleConfig(
            reason="docs",
            steps=[NudgeStep(offset_seconds=0, channels=[Channel.WHATSAPP], template_key="t")],
        )
    )
    service = build_nudge_service(config_provider=provider, clock=clock)
    await service.start_sequence("docs", {Channel.WHATSAPP: "+97455500001"})

    # One reminder is due at base time (offset 0); the job drains it.
    assert await jobs.run_due_nudges(service) == 1
    # Nothing left due on the next tick.
    assert await jobs.run_due_nudges(service) == 0


async def test_recover_workflows_no_pending_returns_zero():
    platform = build_onboarding_platform()
    assert await jobs.recover_workflows(platform) == 0


async def test_sweep_workflow_timeouts_no_waiting_returns_zero():
    platform = build_onboarding_platform()
    assert await jobs.sweep_workflow_timeouts(platform) == 0
