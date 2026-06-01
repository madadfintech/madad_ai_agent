"""Communication domain models (persisted in the ``communication`` schema).

These hold orchestration/transport state only — message content, delivery state,
conversation threading, and attachment *metadata*. Attachment bytes live in
object storage and are handled by the Document Intelligence service; here we keep
only references.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field

from app.shared.workflow.enums import Channel
from app.shared.workflow.utils import new_id, utcnow

from .enums import DEFAULT_LOCALE, Locale, MessageDirection, MessageStatus, MessageType


class Attachment(BaseModel):
    """Metadata for a message attachment (no bytes — only references)."""

    attachment_id: str = Field(default_factory=lambda: new_id("att"))
    filename: str
    content_type: str | None = None
    size_bytes: int | None = None
    direction: MessageDirection = MessageDirection.INBOUND
    # Provider media id (from the channel/MCP layer) for inbound fetch.
    provider_ref: str | None = None
    # Object-storage key once staged by the Document Intelligence service.
    storage_ref: str | None = None
    checksum: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class Conversation(BaseModel):
    """A conversation thread, one open thread per channel-identity.

    The thread links to the workflow session (same channel-identity key), giving
    conversation continuity across reconnects.
    """

    conversation_id: str = Field(default_factory=lambda: new_id("conv"))
    channel: Channel
    identity: str
    session_id: str | None = None
    locale: Locale = DEFAULT_LOCALE
    is_open: bool = True
    # Email threading anchor (root Message-ID); WhatsApp leaves this unset.
    external_thread_ref: str | None = None
    subject: str | None = None
    message_count: int = 0
    last_message_at: datetime | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime = Field(default_factory=utcnow)
    updated_at: datetime = Field(default_factory=utcnow)


class Message(BaseModel):
    """A single inbound or outbound message."""

    message_id: str = Field(default_factory=lambda: new_id("msg"))
    conversation_id: str
    channel: Channel
    identity: str
    direction: MessageDirection
    type: MessageType = MessageType.TEXT
    status: MessageStatus = MessageStatus.RECEIVED
    locale: Locale = DEFAULT_LOCALE

    # Rendered, ready-to-send (or received) text.
    text: str | None = None
    # If rendered from a template: the source key + variables (for audit/replay).
    template_key: str | None = None
    variables: dict[str, Any] = Field(default_factory=dict)
    attachments: list[Attachment] = Field(default_factory=list)

    # Provider/MCP message id (send response, or inbound provider id).
    provider_message_id: str | None = None
    in_reply_to: str | None = None  # message_id this replies to
    session_id: str | None = None
    correlation_id: str | None = None

    attempts: int = 0
    last_error: str | None = None

    created_at: datetime = Field(default_factory=utcnow)
    updated_at: datetime = Field(default_factory=utcnow)
    sent_at: datetime | None = None
    delivered_at: datetime | None = None
    read_at: datetime | None = None

    metadata: dict[str, Any] = Field(default_factory=dict)
