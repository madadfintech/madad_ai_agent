"""Shared communication DTOs.

Request/response shapes for the communication API and for other services (e.g. a
workflow node asking the service to send a message). Kept separate from the
internal domain models so the wire contract can evolve independently.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field, model_validator

from app.shared.workflow.enums import Channel

from .enums import Locale, MessageDirection, MessageStatus, MessageType
from .models import Attachment, Message


class AttachmentDTO(BaseModel):
    filename: str
    content_type: str | None = None
    size_bytes: int | None = None
    provider_ref: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class InboundMessageDTO(BaseModel):
    """A normalized inbound message handed to us by the MCP layer.

    NOTE: this is already normalized — the MCP cluster owns parsing raw Meta /
    SendGrid webhooks. We never see provider-specific payloads here.
    """

    channel: Channel
    identity: str  # WhatsApp E.164 or email address
    text: str | None = None
    type: MessageType = MessageType.TEXT
    attachments: list[AttachmentDTO] = Field(default_factory=list)
    provider_message_id: str | None = None
    in_reply_to: str | None = None
    locale: Locale | None = None
    subject: str | None = None  # email
    external_thread_ref: str | None = None  # email Message-ID / References root
    correlation_id: str | None = None
    raw: dict[str, Any] = Field(default_factory=dict)


class OutboundMessageRequest(BaseModel):
    """A request to send an outbound message.

    Provide either ``text`` (already-composed) or ``template_key`` + ``variables``
    (rendered from CMS). Exactly one is required.
    """

    channel: Channel
    identity: str
    text: str | None = None
    template_key: str | None = None
    variables: dict[str, Any] = Field(default_factory=dict)
    attachments: list[AttachmentDTO] = Field(default_factory=list)
    locale: Locale | None = None
    in_reply_to: str | None = None
    correlation_id: str | None = None

    @model_validator(mode="after")
    def _exactly_one_body(self) -> OutboundMessageRequest:
        if bool(self.text) == bool(self.template_key):
            raise ValueError("Provide exactly one of 'text' or 'template_key'.")
        return self


class DeliveryStatusUpdate(BaseModel):
    """A delivery callback (forwarded by the MCP layer)."""

    status: MessageStatus
    provider_message_id: str | None = None
    message_id: str | None = None
    error: str | None = None

    @model_validator(mode="after")
    def _need_an_id(self) -> DeliveryStatusUpdate:
        if not (self.provider_message_id or self.message_id):
            raise ValueError("One of 'provider_message_id' or 'message_id' is required.")
        return self


class MessageDTO(BaseModel):
    """Read model returned by the API."""

    message_id: str
    conversation_id: str
    channel: Channel
    identity: str
    direction: MessageDirection
    type: MessageType
    status: MessageStatus
    locale: Locale
    text: str | None
    template_key: str | None
    attachments: list[AttachmentDTO]
    provider_message_id: str | None
    attempts: int
    last_error: str | None

    @classmethod
    def from_model(cls, message: Message) -> MessageDTO:
        return cls(
            message_id=message.message_id,
            conversation_id=message.conversation_id,
            channel=message.channel,
            identity=message.identity,
            direction=message.direction,
            type=message.type,
            status=message.status,
            locale=message.locale,
            text=message.text,
            template_key=message.template_key,
            attachments=[
                AttachmentDTO(
                    filename=a.filename,
                    content_type=a.content_type,
                    size_bytes=a.size_bytes,
                    provider_ref=a.provider_ref,
                    metadata=a.metadata,
                )
                for a in message.attachments
            ],
            provider_message_id=message.provider_message_id,
            attempts=message.attempts,
            last_error=message.last_error,
        )


def to_attachment(dto: AttachmentDTO, direction: MessageDirection) -> Attachment:
    """Map an attachment DTO to a domain attachment with a direction."""

    return Attachment(
        filename=dto.filename,
        content_type=dto.content_type,
        size_bytes=dto.size_bytes,
        provider_ref=dto.provider_ref,
        direction=direction,
        metadata=dto.metadata,
    )
