"""Real-service adapters for the onboarding ports.

Bridge the workflow's ports to the already-built services. Madad/Tess have no
real adapter yet (MCP-blocked); the Document adapter needs media fetch (also MCP)
so the in-memory intake is used until then.
"""

from __future__ import annotations

from typing import Any

from app.services.communication.schemas import OutboundMessageRequest
from app.services.communication.service import CommunicationService
from app.services.nudge.service import NudgeService
from app.shared.workflow.enums import Channel

from .ports import Messenger, Reminders


class CommunicationMessenger(Messenger):
    """Sends outbound messages through the Communication service (renders CMS
    templates, dispatches via MCP)."""

    def __init__(self, comms: CommunicationService) -> None:
        self._comms = comms

    async def send(
        self,
        *,
        channel: Channel,
        identity: str,
        template_key: str,
        variables: dict[str, Any] | None = None,
        locale: str = "en",
    ) -> None:
        from app.shared.i18n import Locale

        await self._comms.send(
            OutboundMessageRequest(
                channel=channel,
                identity=identity,
                template_key=template_key,
                variables=variables or {},
                locale=Locale(locale),
            )
        )


class NudgeReminders(Reminders):
    """Schedules/suppresses reminder sequences through the Nudge service."""

    def __init__(self, nudge: NudgeService) -> None:
        self._nudge = nudge

    async def schedule(
        self,
        reason: str,
        *,
        channel: Channel,
        identity: str,
        target_ref: str | None,
        variables: dict[str, Any] | None = None,
    ) -> None:
        from app.services.nudge.errors import ScheduleNotFoundError

        try:
            await self._nudge.start_sequence(
                reason,
                {channel: identity},
                variables=variables,
                target_ref=target_ref,
            )
        except ScheduleNotFoundError:
            # A reason with no configured schedule must not break onboarding.
            return

    async def suppress(self, *, target_ref: str | None) -> None:
        if target_ref is not None:
            await self._nudge.suppress_matching(target_ref=target_ref)
