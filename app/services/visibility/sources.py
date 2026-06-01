"""Message source — pulls conversation message content for replay.

Conversation replay merges the activity timeline with the actual message text.
Rather than duplicate every message into this service, replay pulls content from
the Communication service through this port (kept optional/decoupled). The
authoritative messages stay in Communication.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import TYPE_CHECKING

from .models import ReplayMessage

if TYPE_CHECKING:  # pragma: no cover
    from app.services.communication.service import CommunicationService


class MessageSource(ABC):
    @abstractmethod
    async def get_conversation_messages(self, conversation_id: str) -> list[ReplayMessage]: ...


class InMemoryMessageSource(MessageSource):
    """Test/dev source backed by a dict of conversation_id -> messages."""

    def __init__(self) -> None:
        self._messages: dict[str, list[ReplayMessage]] = {}

    def add(self, conversation_id: str, message: ReplayMessage) -> None:
        self._messages.setdefault(conversation_id, []).append(message)

    async def get_conversation_messages(self, conversation_id: str) -> list[ReplayMessage]:
        return list(self._messages.get(conversation_id, []))


class CommunicationMessageSource(MessageSource):
    """Pulls message content from the Communication service for replay."""

    def __init__(self, comms: CommunicationService) -> None:
        self._comms = comms

    async def get_conversation_messages(self, conversation_id: str) -> list[ReplayMessage]:
        messages = await self._comms.get_messages(conversation_id)
        return [
            ReplayMessage(
                message_id=m.message_id,
                direction=str(m.direction),
                type=str(m.type),
                status=str(m.status),
                text=m.text,
                channel=str(m.channel),
                occurred_at=m.created_at,
            )
            for m in messages
        ]


class NullMessageSource(MessageSource):
    """No message content (replay falls back to the activity timeline only)."""

    async def get_conversation_messages(self, conversation_id: str) -> list[ReplayMessage]:
        return []
