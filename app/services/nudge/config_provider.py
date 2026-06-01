"""Reminder schedule provider — makes nudge timings runtime-configurable.

Schedules live in the CMS (``NUDGE`` config kind), so MADAD can change reminder
timing/channels/content without a deploy. The service depends on this port; the
CMS adapter reads from the CMS service, and an in-memory provider is used in tests.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import TYPE_CHECKING

from .errors import ScheduleNotFoundError
from .models import NudgeScheduleConfig

if TYPE_CHECKING:  # pragma: no cover
    from app.services.cms.service import CmsService


class NudgeConfigProvider(ABC):
    """Port returning the reminder schedule for a reason."""

    @abstractmethod
    async def get_schedule(self, reason: str) -> NudgeScheduleConfig: ...


class InMemoryNudgeConfigProvider(NudgeConfigProvider):
    """In-memory schedules for tests/dev."""

    def __init__(self) -> None:
        self._schedules: dict[str, NudgeScheduleConfig] = {}

    def add(self, schedule: NudgeScheduleConfig) -> None:
        self._schedules[schedule.reason] = schedule

    async def get_schedule(self, reason: str) -> NudgeScheduleConfig:
        schedule = self._schedules.get(reason)
        if schedule is None:
            raise ScheduleNotFoundError(
                f"No nudge schedule for reason {reason!r}", details={"reason": reason}
            )
        return schedule


class CmsNudgeConfigProvider(NudgeConfigProvider):
    """Reads reminder schedules from the CMS (``NUDGE`` kind)."""

    def __init__(self, cms: CmsService) -> None:
        self._cms = cms

    async def get_schedule(self, reason: str) -> NudgeScheduleConfig:
        from app.services.cms.enums import ConfigKind

        record = await self._cms.get(ConfigKind.NUDGE, reason)
        if record is None:
            raise ScheduleNotFoundError(
                f"No nudge schedule for reason {reason!r}", details={"reason": reason}
            )
        return NudgeScheduleConfig.from_cms_value(reason, record.value)
