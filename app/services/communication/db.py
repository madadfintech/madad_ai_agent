"""PostgreSQL-backed communication stores (message + conversation)."""

from __future__ import annotations

from typing import Any

from sqlalchemy import Boolean, String, select
from sqlalchemy.orm import Mapped, mapped_column

from app.shared.db import Base, Database
from app.shared.workflow.enums import Channel

from .enums import MessageStatus
from .models import Conversation, Message
from .persistence import ConversationStore, MessageStore


class MessageRow(Base):
    __tablename__ = "messages"
    __table_args__ = {"schema": "communication"}

    message_id: Mapped[str] = mapped_column(String, primary_key=True)
    conversation_id: Mapped[str] = mapped_column(String, index=True)
    provider_message_id: Mapped[str | None] = mapped_column(String, nullable=True, index=True)
    status: Mapped[str] = mapped_column(String, index=True)
    created_at: Mapped[str] = mapped_column(String, index=True)
    data: Mapped[dict[str, Any]] = mapped_column()


class ConversationRow(Base):
    __tablename__ = "conversations"
    __table_args__ = {"schema": "communication"}

    conversation_id: Mapped[str] = mapped_column(String, primary_key=True)
    channel: Mapped[str] = mapped_column(String, index=True)
    identity: Mapped[str] = mapped_column(String, index=True)
    is_open: Mapped[bool] = mapped_column(Boolean, index=True)
    data: Mapped[dict[str, Any]] = mapped_column()


class PostgresMessageStore(MessageStore):
    def __init__(self, db: Database) -> None:
        self._db = db

    async def create(self, message: Message) -> Message:
        async with self._db.session() as session:
            session.add(_message_row(message))
        return message

    async def get(self, message_id: str) -> Message | None:
        async with self._db.session() as session:
            row = await session.get(MessageRow, message_id)
            return Message.model_validate(row.data) if row else None

    async def get_by_provider_id(self, provider_message_id: str) -> Message | None:
        async with self._db.session() as session:
            row = (
                await session.execute(
                    select(MessageRow).where(
                        MessageRow.provider_message_id == provider_message_id
                    )
                )
            ).scalars().first()
            return Message.model_validate(row.data) if row else None

    async def save(self, message: Message) -> Message:
        from app.shared.workflow.utils import utcnow

        message.updated_at = utcnow()
        async with self._db.session() as session:
            row = await session.get(MessageRow, message.message_id)
            if row is None:
                session.add(_message_row(message))
            else:
                row.provider_message_id = message.provider_message_id
                row.status = str(message.status)
                row.data = message.model_dump(mode="json")
        return message

    async def list_by_conversation(self, conversation_id: str) -> list[Message]:
        async with self._db.session() as session:
            rows = (
                await session.execute(
                    select(MessageRow)
                    .where(MessageRow.conversation_id == conversation_id)
                    .order_by(MessageRow.created_at)
                )
            ).scalars().all()
            return [Message.model_validate(r.data) for r in rows]

    async def list_failed(self, limit: int = 100) -> list[Message]:
        async with self._db.session() as session:
            rows = (
                await session.execute(
                    select(MessageRow)
                    .where(MessageRow.status == str(MessageStatus.FAILED))
                    .order_by(MessageRow.created_at)
                    .limit(limit)
                )
            ).scalars().all()
            return [Message.model_validate(r.data) for r in rows]


class PostgresConversationStore(ConversationStore):
    def __init__(self, db: Database) -> None:
        self._db = db

    async def get(self, conversation_id: str) -> Conversation | None:
        async with self._db.session() as session:
            row = await session.get(ConversationRow, conversation_id)
            return Conversation.model_validate(row.data) if row else None

    async def find_open(self, channel: Channel, identity: str) -> Conversation | None:
        async with self._db.session() as session:
            row = (
                await session.execute(
                    select(ConversationRow).where(
                        ConversationRow.channel == str(channel),
                        ConversationRow.identity == identity,
                        ConversationRow.is_open.is_(True),
                    )
                )
            ).scalars().first()
            return Conversation.model_validate(row.data) if row else None

    async def save(self, conversation: Conversation) -> Conversation:
        from app.shared.workflow.utils import utcnow

        conversation.updated_at = utcnow()
        async with self._db.session() as session:
            row = await session.get(ConversationRow, conversation.conversation_id)
            if row is None:
                session.add(_conversation_row(conversation))
            else:
                row.is_open = conversation.is_open
                row.data = conversation.model_dump(mode="json")
        return conversation


def _message_row(message: Message) -> MessageRow:
    return MessageRow(
        message_id=message.message_id,
        conversation_id=message.conversation_id,
        provider_message_id=message.provider_message_id,
        status=str(message.status),
        created_at=message.created_at.isoformat(),
        data=message.model_dump(mode="json"),
    )


def _conversation_row(conversation: Conversation) -> ConversationRow:
    return ConversationRow(
        conversation_id=conversation.conversation_id,
        channel=str(conversation.channel),
        identity=conversation.identity,
        is_open=conversation.is_open,
        data=conversation.model_dump(mode="json"),
    )
