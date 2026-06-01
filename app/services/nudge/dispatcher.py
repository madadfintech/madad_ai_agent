"""Notification dispatcher — delivers a nudge through the Communication service.

The nudge service does NOT call WhatsApp/Email or MCP directly. It triggers an
outbound send via the Communication service (which renders the CMS template and
dispatches through the MCP gateway). This port is the seam; a transient delivery
failure raises :class:`NotificationDispatchError` so the worker retries.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import TYPE_CHECKING, Any

from app.shared.i18n import Locale
from app.shared.workflow.enums import Channel

from .errors import NotificationDispatchError

if TYPE_CHECKING:  # pragma: no cover
    from app.services.communication.service import CommunicationService


class NotificationDispatcher(ABC):
    """Port for delivering a single notification on one channel."""

    @abstractmethod
    async def dispatch(
        self,
        channel: Channel,
        identity: str,
        *,
        template_key: str,
        variables: dict[str, Any],
        locale: Locale,
        correlation_id: str | None = None,
    ) -> None:
        """Send the notification, or raise :class:`NotificationDispatchError`."""


class InMemoryNotificationDispatcher(NotificationDispatcher):
    """Records dispatches; can simulate transient failures for retry tests."""

    def __init__(self, *, fail_times: int = 0) -> None:
        self.sent: list[dict[str, Any]] = []
        self._fail_times = fail_times
        self._calls = 0

    async def dispatch(
        self,
        channel: Channel,
        identity: str,
        *,
        template_key: str,
        variables: dict[str, Any],
        locale: Locale,
        correlation_id: str | None = None,
    ) -> None:
        self._calls += 1
        if self._calls <= self._fail_times:
            raise NotificationDispatchError(f"simulated dispatch failure #{self._calls}")
        self.sent.append(
            {
                "channel": channel,
                "identity": identity,
                "template_key": template_key,
                "variables": variables,
                "locale": locale,
            }
        )


class CommunicationNotificationDispatcher(NotificationDispatcher):
    """Delivers via the Communication service (template render + MCP dispatch)."""

    def __init__(self, comms: CommunicationService) -> None:
        self._comms = comms

    async def dispatch(
        self,
        channel: Channel,
        identity: str,
        *,
        template_key: str,
        variables: dict[str, Any],
        locale: Locale,
        correlation_id: str | None = None,
    ) -> None:
        from app.services.communication.enums import MessageStatus
        from app.services.communication.schemas import OutboundMessageRequest

        message = await self._comms.send(
            OutboundMessageRequest(
                channel=channel,
                identity=identity,
                template_key=template_key,
                variables=variables,
                locale=locale,
                correlation_id=correlation_id,
            )
        )
        if message.status == MessageStatus.FAILED:
            raise NotificationDispatchError(
                f"Communication send failed: {message.last_error}",
                details={"message_id": message.message_id},
            )
