"""Fixtures for nudge service tests (all in-memory, deterministic clock)."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import pytest

from app.services.nudge import (
    InMemoryNotificationDispatcher,
    InMemoryNudgeConfigProvider,
    InMemoryNudgeEventBus,
    NudgeConfig,
    NudgeScheduleConfig,
    NudgeService,
    NudgeStep,
    build_nudge_service,
)
from app.shared.workflow.context import Clock
from app.shared.workflow.enums import Channel


class FakeClock(Clock):
    def __init__(self, start: datetime | None = None) -> None:
        self._now = start or datetime(2026, 5, 21, 12, 0, 0, tzinfo=UTC)

    def now(self) -> datetime:
        return self._now

    def advance(self, seconds: float) -> None:
        self._now += timedelta(seconds=seconds)


@dataclass
class Harness:
    service: NudgeService
    provider: InMemoryNudgeConfigProvider
    dispatcher: InMemoryNotificationDispatcher
    events: InMemoryNudgeEventBus
    clock: FakeClock

    def event_types(self) -> list[str]:
        return [str(e.type) for e in self.events.history]

    def add_schedule(self, reason: str, *steps: NudgeStep, max_attempts: int = 3) -> None:
        self.provider.add(
            NudgeScheduleConfig(reason=reason, steps=list(steps), max_attempts=max_attempts)
        )


def step(
    offset: int, *channels: Channel, template: str = "nudge.tpl", escalate: bool = False
) -> NudgeStep:
    return NudgeStep(
        offset_seconds=offset,
        channels=list(channels) or [Channel.WHATSAPP],
        template_key=template,
        escalate=escalate,
    )


@pytest.fixture
def make_harness() -> Callable[..., Harness]:
    def _make(*, fail_times: int = 0) -> Harness:
        clock = FakeClock()
        provider = InMemoryNudgeConfigProvider()
        dispatcher = InMemoryNotificationDispatcher(fail_times=fail_times)
        events = InMemoryNudgeEventBus()
        service = build_nudge_service(
            config_provider=provider,
            dispatcher=dispatcher,
            events=events,
            config=NudgeConfig(retry_base_delay=1.0, retry_jitter=False),
            clock=clock,
        )
        return Harness(service, provider, dispatcher, events, clock)

    return _make


@pytest.fixture
def harness(make_harness: Callable[..., Harness]) -> Harness:
    return make_harness()
