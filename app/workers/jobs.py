"""Periodic platform jobs (plain async; no Celery dependency).

Each job is the unit of work a Celery beat tick performs. They default to the
process-singleton services but accept explicit instances for testing. Returning
a count keeps the Celery task result small and useful for monitoring.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:  # pragma: no cover
    from app.services.nudge.service import NudgeService
    from app.services.workflow.deps import OnboardingPlatform


async def run_due_nudges(service: NudgeService | None = None) -> int:
    """Drain reminders due now and dispatch them (Nudge worker tick)."""

    if service is None:
        from app.services.nudge.deps import get_nudge_service

        service = get_nudge_service()
    processed = await service.run_due()
    return len(processed)


async def recover_workflows(platform: OnboardingPlatform | None = None) -> int:
    """Re-drive workflow runs left mid-step by a crash."""

    if platform is None:
        from app.services.workflow.deps import get_onboarding_platform

        platform = get_onboarding_platform()
    results = await platform.runtime.recover()
    return len(results)


async def sweep_workflow_timeouts(platform: OnboardingPlatform | None = None) -> int:
    """Time out runs whose waiting session has lapsed; emit expiry events."""

    if platform is None:
        from app.services.workflow.deps import get_onboarding_platform

        platform = get_onboarding_platform()
    expired = await platform.runtime.recovery.sweep_timeouts()
    return len(expired)
