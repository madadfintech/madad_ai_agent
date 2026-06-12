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

    async def send_cta_url(
        self,
        *,
        channel: Channel,
        identity: str,
        template_key: str,
        button_text: str,
        button_url: str,
        variables: dict[str, Any] | None = None,
        locale: str = "en",
    ) -> bool:
        from app.services.communication.models import MessageStatus  # type: ignore[attr-defined]
        from app.shared.i18n import Locale

        message = await self._comms.send(
            OutboundMessageRequest(
                channel=channel,
                identity=identity,
                template_key=template_key,
                variables=variables or {},
                locale=Locale(locale),
                metadata={
                    "cta": {"button_text": button_text, "button_url": button_url}
                },
            )
        )
        # Only report success if the interactive send actually went out — the
        # caller falls back to a plain-text message otherwise (e.g. when the
        # backend interactive endpoint / MCP tool is not yet live).
        return getattr(message, "status", None) == MessageStatus.SENT

    async def send_reply_buttons(
        self,
        *,
        channel: Channel,
        identity: str,
        template_key: str,
        buttons: list[tuple[str, str]],
        variables: dict[str, Any] | None = None,
        locale: str = "en",
    ) -> bool:
        """Send up to 3 interactive reply (quick-reply) buttons. ``buttons`` is
        a list of ``(id, title)`` pairs. Returns True only if the interactive
        send went out; the caller falls back to plain text otherwise (backend
        ``interactive-buttons`` endpoint / MCP tool not yet live)."""
        from app.services.communication.models import MessageStatus  # type: ignore[attr-defined]
        from app.shared.i18n import Locale

        message = await self._comms.send(
            OutboundMessageRequest(
                channel=channel,
                identity=identity,
                template_key=template_key,
                variables=variables or {},
                locale=Locale(locale),
                metadata={
                    "interactive_buttons": {
                        "buttons": [
                            {"id": bid, "title": title} for bid, title in buttons
                        ]
                    }
                },
            )
        )
        return getattr(message, "status", None) == MessageStatus.SENT

    async def send_template(
        self,
        *,
        channel: Channel,
        identity: str,
        template_name: str,
        template_key: str,
        language_code: str = "en",
        variables: dict[str, Any] | None = None,
        components: list[dict[str, Any]] | None = None,
    ) -> bool:
        from app.services.communication.models import MessageStatus  # type: ignore[attr-defined]
        from app.shared.i18n import Locale

        message = await self._comms.send(
            OutboundMessageRequest(
                channel=channel,
                identity=identity,
                template_key=template_key,
                variables=variables or {},
                locale=Locale(language_code if len(language_code) == 2 else "en"),
                metadata={
                    "whatsapp_template": {
                        "name": template_name,
                        "language": language_code,
                        **({"components": components} if components else {}),
                    }
                },
            )
        )
        return getattr(message, "status", None) == MessageStatus.SENT


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
