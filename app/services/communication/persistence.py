"""Communication persistence ports and in-memory adapters.

Stores live in the ``communication`` schema in production (Postgres adapter lands
with the platform DB foundation). The service depends only on these ports, so the
backend can be swapped without touching orchestration logic.
"""

from __future__ import annotations

import asyncio
from abc import ABC, abstractmethod

from app.shared.workflow.enums import Channel
from app.shared.workflow.utils import utcnow

from .enums import MessageStatus
from .errors import ConversationNotFoundError, MessageNotFoundError
from .models import Conversation, Message


class ConversationStore(ABC):
    @abstractmethod
    async def get(self, conversation_id: str) -> Conversation | None: ...

    @abstractmethod
    async def find_open(self, channel: Channel, identity: str) -> Conversation | None: ...

    @abstractmethod
    async def save(self, conversation: Conversation) -> Conversation: ...


class MessageStore(ABC):
    @abstractmethod
    async def create(self, message: Message) -> Message: ...

    @abstractmethod
    async def get(self, message_id: str) -> Message | None: ...

    @abstractmethod
    async def get_by_provider_id(self, provider_message_id: str) -> Message | None: ...

    @abstractmethod
    async def save(self, message: Message) -> Message: ...

    @abstractmethod
    async def list_by_conversation(self, conversation_id: str) -> list[Message]: ...

    @abstractmethod
    async def list_failed(self, limit: int = 100) -> list[Message]: ...


class InMemoryConversationStore(ConversationStore):
    def __init__(self) -> None:
        self._by_id: dict[str, Conversation] = {}
        self._open_by_key: dict[tuple[Channel, str], str] = {}
        self._lock = asyncio.Lock()

    async def get(self, conversation_id: str) -> Conversation | None:
        async with self._lock:
            stored = self._by_id.get(conversation_id)
            return stored.model_copy(deep=True) if stored else None

    async def find_open(self, channel: Channel, identity: str) -> Conversation | None:
        async with self._lock:
            conv_id = self._open_by_key.get((channel, identity))
            if conv_id is None:
                return None
            stored = self._by_id.get(conv_id)
            return stored.model_copy(deep=True) if stored else None

    async def save(self, conversation: Conversation) -> Conversation:
        conversation.updated_at = utcnow()
        async with self._lock:
            self._by_id[conversation.conversation_id] = conversation.model_copy(deep=True)
            key = (conversation.channel, conversation.identity)
            if conversation.is_open:
                self._open_by_key[key] = conversation.conversation_id
            elif self._open_by_key.get(key) == conversation.conversation_id:
                del self._open_by_key[key]
        return conversation


class InMemoryMessageStore(MessageStore):
    def __init__(self) -> None:
        self._by_id: dict[str, Message] = {}
        self._by_provider: dict[str, str] = {}
        self._lock = asyncio.Lock()

    async def create(self, message: Message) -> Message:
        async with self._lock:
            self._by_id[message.message_id] = message.model_copy(deep=True)
            if message.provider_message_id:
                self._by_provider[message.provider_message_id] = message.message_id
        return message

    async def get(self, message_id: str) -> Message | None:
        async with self._lock:
            stored = self._by_id.get(message_id)
            return stored.model_copy(deep=True) if stored else None

    async def get_by_provider_id(self, provider_message_id: str) -> Message | None:
        async with self._lock:
            mid = self._by_provider.get(provider_message_id)
            if mid is None:
                return None
            stored = self._by_id.get(mid)
            return stored.model_copy(deep=True) if stored else None

    async def save(self, message: Message) -> Message:
        message.updated_at = utcnow()
        async with self._lock:
            self._by_id[message.message_id] = message.model_copy(deep=True)
            if message.provider_message_id:
                self._by_provider[message.provider_message_id] = message.message_id
        return message

    async def list_by_conversation(self, conversation_id: str) -> list[Message]:
        async with self._lock:
            msgs = [
                m.model_copy(deep=True)
                for m in self._by_id.values()
                if m.conversation_id == conversation_id
            ]
        msgs.sort(key=lambda m: m.created_at)
        return msgs

    async def list_failed(self, limit: int = 100) -> list[Message]:
        async with self._lock:
            msgs = [
                m.model_copy(deep=True)
                for m in self._by_id.values()
                if m.status == MessageStatus.FAILED
            ]
        msgs.sort(key=lambda m: m.updated_at)
        return msgs[:limit]


async def require_message(store: MessageStore, message_id: str) -> Message:
    message = await store.get(message_id)
    if message is None:
        raise MessageNotFoundError(f"Message {message_id!r} not found")
    return message


async def require_conversation(store: ConversationStore, conversation_id: str) -> Conversation:
    conversation = await store.get(conversation_id)
    if conversation is None:
        raise ConversationNotFoundError(f"Conversation {conversation_id!r} not found")
    return conversation
