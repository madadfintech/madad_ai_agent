"""Fixtures for workflow runtime tests.

Every runtime is built with in-memory adapters and a no-op sleep so retries don't
actually wait. ``make_runtime`` lets a test tweak workflow settings (timeouts,
retry policy, session TTL).
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from app.core.config import Settings
from app.shared.workflow import WorkflowRuntime, build_runtime
from app.shared.workflow.context import Clock


async def _no_sleep(_delay: float) -> None:
    """Retry backoff that returns immediately."""

    return None


class FakeClock(Clock):
    """Controllable clock for deterministic time-based tests."""

    def __init__(self, start: datetime | None = None) -> None:
        self._now = start or datetime(2026, 5, 20, 12, 0, 0, tzinfo=UTC)

    def now(self) -> datetime:
        return self._now

    def advance(self, seconds: float) -> None:
        self._now += timedelta(seconds=seconds)


@pytest.fixture
def fake_clock() -> FakeClock:
    return FakeClock()


@pytest.fixture
def make_runtime() -> Callable[..., WorkflowRuntime]:
    def _make(*, clock: Clock | None = None, **workflow_overrides: Any) -> WorkflowRuntime:
        settings = Settings()
        for key, value in workflow_overrides.items():
            setattr(settings.workflow, key, value)
        return build_runtime(settings, clock=clock, sleep=_no_sleep)

    return _make


@pytest.fixture
def runtime(make_runtime: Callable[..., WorkflowRuntime]) -> WorkflowRuntime:
    return make_runtime()
