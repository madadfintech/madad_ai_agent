"""Fixtures for Operational Visibility tests."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from app.services.visibility import (
    ActivityEvent,
    ActivitySource,
    OperationalVisibilityService,
    build_visibility_service,
)

BASE = datetime(2026, 5, 21, 12, 0, 0, tzinfo=UTC)


def at(seconds: float) -> datetime:
    return BASE + timedelta(seconds=seconds)


def activity(source: ActivitySource, type: str, *, t: float = 0, **refs: Any) -> ActivityEvent:
    return ActivityEvent(source=source, type=type, occurred_at=at(t), **refs)


@pytest.fixture
def service() -> OperationalVisibilityService:
    return build_visibility_service()
