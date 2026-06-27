"""Communication orchestration layer.

Ties together persistence, templating, the MCP gateway, events, and audit into
the two core flows:

* **inbound** — persist a normalized inbound message, link its conversation
  thread, record attachment metadata, emit events, and hand off to the
  conversation dispatcher (workflow seam).
* **outbound** — resolve/render content, persist, dispatch through the gateway
  with retry + backoff, and track delivery status.

Async-first, event-driven, and dependency-injected so it runs with in-memory
adapters in tests and Redis/Postgres/MCP adapters in production.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from app.core.logging import get_logger
from app.shared.workflow.enums import Channel
from app.shared.workflow.utils import compute_backoff, utcnow

from .audit import CommunicationAuditLogger
from .dispatcher import ConversationDispatcher
from .enums import (
    DEFAULT_LOCALE,
    Locale,
    MessageDirection,
    MessageStatus,
    MessageType,
    can_transition,
)
from .errors import GatewayError, InvalidMessageStatusError
from .events import (
    CommunicationEvent,
    CommunicationEventBus,
    CommunicationEventType,
)
from .gateway import CommunicationGateway
from .models import Conversation, Message
from .persistence import ConversationStore, MessageStore
from .schemas import (
    DeliveryStatusUpdate,
    InboundMessageDTO,
    OutboundMessageRequest,
    to_attachment,
)
from .templating import TemplateRenderer

SleepFn = Callable[[float], Awaitable[None]]


@dataclass
class CommunicationConfig:
    """Outbound dispatch + rendering tunables."""

    retry_max_attempts: int = 3
    retry_base_delay: float = 0.5
    retry_max_delay: float = 30.0
    retry_jitter: bool = True
    default_locale: Locale = DEFAULT_LOCALE


_STATUS_EVENT: dict[MessageStatus, CommunicationEventType] = {
    MessageStatus.SENT: CommunicationEventType.MESSAGE_SENT,
    MessageStatus.DELIVERED: CommunicationEventType.MESSAGE_DELIVERED,
    MessageStatus.READ: CommunicationEventType.MESSAGE_READ,
    MessageStatus.FAILED: CommunicationEventType.MESSAGE_FAILED,
}


class CommunicationService:
    """Inbound/outbound communication orchestration."""

    def __init__(
        self,
        *,
        messages: MessageStore,
        conversations: ConversationStore,
        gateway: CommunicationGateway,
        renderer: TemplateRenderer,
        events: CommunicationEventBus,
        audit: CommunicationAuditLogger,
        dispatcher: ConversationDispatcher | None = None,
        config: CommunicationConfig | None = None,
        sleep: SleepFn | None = None,
        logger: Any | None = None,
    ) -> None:
        self._messages = messages
        self._conversations = conversations
        self._gateway = gateway
        self._renderer = renderer
        self._events = events
        self._audit = audit
        self._dispatcher = dispatcher
        self._config = config or CommunicationConfig()
        self._sleep: SleepFn = sleep or asyncio.sleep
        self._log = logger or get_logger("communication.service")

    @property
    def events(self) -> CommunicationEventBus:
        """The in-process event bus (for forwarding onto the unified bus)."""

        return self._events

    # -- inbound --------------------------------------------------------------

    async def ingest_inbound(self, dto: InboundMessageDTO) -> Message:
        """Persist a normalized inbound message and hand off for processing.

        Idempotent on ``provider_message_id``: a duplicate delivery returns the
        already-stored message instead of creating another.
        """

        if dto.provider_message_id:
            existing = await self._messages.get_by_provider_id(dto.provider_message_id)
            if existing is not None:
                self._log.info(
                    "communication.inbound.duplicate",
                    provider_message_id=dto.provider_message_id,
                    message_id=existing.message_id,
                )
                return existing

        conversation = await self._resolve_conversation(
            dto.channel,
            dto.identity,
            locale=dto.locale,
            subject=dto.subject,
            external_thread_ref=dto.external_thread_ref,
        )
        locale = dto.locale or conversation.locale
        attachments = [to_attachment(a, MessageDirection.INBOUND) for a in dto.attachments]
        msg_type = dto.type
        if attachments and dto.type == MessageType.TEXT and not dto.text:
            msg_type = MessageType.MEDIA

        message = Message(
            conversation_id=conversation.conversation_id,
            channel=dto.channel,
            identity=dto.identity,
            direction=MessageDirection.INBOUND,
            type=msg_type,
            status=MessageStatus.RECEIVED,
            locale=locale,
            text=dto.text,
            attachments=attachments,
            provider_message_id=dto.provider_message_id,
            in_reply_to=dto.in_reply_to,
            session_id=conversation.session_id,
            correlation_id=dto.correlation_id,
            metadata={"raw": dto.raw} if dto.raw else {},
        )
        await self._messages.create(message)
        await self._touch_conversation(conversation, locale=dto.locale)

        await self._emit(CommunicationEventType.MESSAGE_RECEIVED, conversation, message)
        for attachment in attachments:
            await self._emit(
                CommunicationEventType.ATTACHMENT_RECEIVED,
                conversation,
                message,
                payload={
                    "attachment_id": attachment.attachment_id,
                    "filename": attachment.filename,
                },
            )
        await self._audit.record(
            conversation.conversation_id,
            "inbound_received",
            message_id=message.message_id,
            detail={"type": str(msg_type), "attachments": len(attachments)},
        )

        if self._dispatcher is not None:
            await self._dispatcher.on_inbound(message)

        return message

    # -- outbound -------------------------------------------------------------

    async def send(self, request: OutboundMessageRequest) -> Message:
        """Render (if templated), persist, and dispatch an outbound message."""

        conversation = await self._resolve_conversation(request.channel, request.identity)
        locale = request.locale or conversation.locale

        text: str | None
        # Email-channel subject (UAT 2026-06-28, Ishan #A). When the template
        # carries a ``data.subject`` field, it's rendered with the same
        # variable bag the body uses and threaded through ``metadata.subject``
        # so the gateway picks it up instead of the default. Non-template
        # sends (plain text) and non-email channels are unaffected — the
        # subject is set on metadata but the WhatsApp gateway ignores it.
        rendered_subject: str | None = None
        if request.template_key:
            text, rendered_subject = await self._renderer.render_with_subject(
                request.template_key, request.variables, locale=locale,
            )
            msg_type = MessageType.TEMPLATE
        else:
            text = request.text
            msg_type = MessageType.MEDIA if request.attachments else MessageType.TEXT

        attachments = [to_attachment(a, MessageDirection.OUTBOUND) for a in request.attachments]
        # Merge subject into metadata without overriding an explicitly
        # supplied one (caller's choice wins). The gateway reads
        # ``metadata.subject`` first, then falls back to its default.
        merged_metadata = dict(request.metadata or {})
        if rendered_subject and "subject" not in merged_metadata:
            merged_metadata["subject"] = rendered_subject
        message = Message(
            conversation_id=conversation.conversation_id,
            channel=request.channel,
            identity=request.identity,
            direction=MessageDirection.OUTBOUND,
            type=msg_type,
            status=MessageStatus.QUEUED,
            locale=locale,
            text=text,
            template_key=request.template_key,
            variables=request.variables,
            attachments=attachments,
            in_reply_to=request.in_reply_to,
            session_id=conversation.session_id,
            correlation_id=request.correlation_id,
            metadata=merged_metadata,
        )
        await self._messages.create(message)
        await self._touch_conversation(conversation)
        await self._emit(CommunicationEventType.MESSAGE_QUEUED, conversation, message)
        await self._audit.record(
            conversation.conversation_id,
            "queued",
            message_id=message.message_id,
            detail={"type": str(msg_type), "template": request.template_key},
        )

        await self._dispatch_with_retry(message, conversation)
        return await self._messages.get(message.message_id) or message

    async def _dispatch_with_retry(self, message: Message, conversation: Conversation) -> None:
        self._transition(message, MessageStatus.SENDING)
        await self._messages.save(message)

        attempts = max(1, self._config.retry_max_attempts)
        for attempt in range(attempts):
            message.attempts += 1
            try:
                result = await self._gateway.send(message)
            except GatewayError as exc:
                message.last_error = str(exc)
                await self._messages.save(message)
                if attempt >= attempts - 1:
                    break
                delay = compute_backoff(
                    attempt + 1,
                    base_delay=self._config.retry_base_delay,
                    max_delay=self._config.retry_max_delay,
                    jitter=self._config.retry_jitter,
                )
                await self._emit(
                    CommunicationEventType.MESSAGE_RETRYING,
                    conversation,
                    message,
                    payload={"attempt": attempt + 1, "delay": delay, "error": str(exc)},
                )
                await self._audit.record(
                    conversation.conversation_id,
                    "retrying",
                    message_id=message.message_id,
                    detail={"attempt": attempt + 1, "error": str(exc)},
                )
                await self._sleep(delay)
                continue

            # Success.
            message.provider_message_id = result.provider_message_id
            message.last_error = None
            message.sent_at = utcnow()
            self._transition(message, MessageStatus.SENT)
            await self._messages.save(message)
            await self._emit(CommunicationEventType.MESSAGE_SENT, conversation, message)
            await self._audit.record(
                conversation.conversation_id,
                "sent",
                message_id=message.message_id,
                detail={"provider_message_id": message.provider_message_id},
            )
            return

        # Exhausted.
        self._transition(message, MessageStatus.FAILED)
        await self._messages.save(message)
        await self._emit(
            CommunicationEventType.MESSAGE_FAILED,
            conversation,
            message,
            payload={"error": message.last_error, "attempts": message.attempts},
        )
        await self._audit.record(
            conversation.conversation_id,
            "failed",
            message_id=message.message_id,
            detail={"error": message.last_error, "attempts": message.attempts},
        )
        self._log.warning(
            "communication.send.failed",
            message_id=message.message_id,
            channel=str(message.channel),
            attempts=message.attempts,
            error=message.last_error,
        )

    # -- delivery status ------------------------------------------------------

    async def update_delivery_status(self, update: DeliveryStatusUpdate) -> Message:
        """Apply a delivery callback (sent/delivered/read/failed)."""

        message = await self._find_message(update)
        conversation = await self._conversations.get(message.conversation_id)

        self._transition(message, update.status)
        now = utcnow()
        if update.status == MessageStatus.DELIVERED:
            message.delivered_at = now
        elif update.status == MessageStatus.READ:
            message.read_at = now
        elif update.status == MessageStatus.FAILED:
            message.last_error = update.error
        await self._messages.save(message)

        if conversation is not None:
            event_type = _STATUS_EVENT.get(update.status)
            if event_type is not None:
                await self._emit(event_type, conversation, message)
            await self._audit.record(
                conversation.conversation_id,
                f"delivery:{update.status}",
                message_id=message.message_id,
                detail={"error": update.error} if update.error else {},
            )
        return message

    # -- reads ----------------------------------------------------------------

    async def get_messages(self, conversation_id: str) -> list[Message]:
        return await self._messages.list_by_conversation(conversation_id)

    async def resolve_conversation(self, channel: Channel, identity: str) -> Conversation:
        return await self._resolve_conversation(channel, identity)

    # -- helpers --------------------------------------------------------------

    async def _resolve_conversation(
        self,
        channel: Channel,
        identity: str,
        *,
        locale: Locale | None = None,
        subject: str | None = None,
        external_thread_ref: str | None = None,
        session_id: str | None = None,
    ) -> Conversation:
        conversation = await self._conversations.find_open(channel, identity)
        if conversation is not None:
            return conversation

        conversation = Conversation(
            channel=channel,
            identity=identity,
            locale=locale or self._config.default_locale,
            subject=subject,
            external_thread_ref=external_thread_ref,
            session_id=session_id,
        )
        await self._conversations.save(conversation)
        await self._emit(CommunicationEventType.CONVERSATION_OPENED, conversation)
        await self._audit.record(conversation.conversation_id, "conversation_opened")
        return conversation

    async def _touch_conversation(
        self, conversation: Conversation, *, locale: Locale | None = None
    ) -> None:
        conversation.message_count += 1
        conversation.last_message_at = utcnow()
        if locale is not None:
            conversation.locale = locale
        await self._conversations.save(conversation)

    async def _find_message(self, update: DeliveryStatusUpdate) -> Message:
        from .errors import MessageNotFoundError

        if update.message_id:
            message = await self._messages.get(update.message_id)
        else:
            assert update.provider_message_id is not None  # validated by the DTO
            message = await self._messages.get_by_provider_id(update.provider_message_id)
        if message is None:
            raise MessageNotFoundError(
                "No message matches the delivery update",
                details={
                    "message_id": update.message_id,
                    "provider_message_id": update.provider_message_id,
                },
            )
        return message

    @staticmethod
    def _transition(message: Message, new_status: MessageStatus) -> None:
        if not can_transition(message.status, new_status):
            raise InvalidMessageStatusError(
                f"Illegal status transition {message.status} -> {new_status}",
                details={"from": str(message.status), "to": str(new_status)},
            )
        message.status = new_status
        message.updated_at = utcnow()

    async def _emit(
        self,
        event_type: CommunicationEventType,
        conversation: Conversation,
        message: Message | None = None,
        *,
        payload: dict[str, Any] | None = None,
    ) -> None:
        await self._events.publish(
            CommunicationEvent(
                type=event_type,
                conversation_id=conversation.conversation_id,
                message_id=message.message_id if message else None,
                channel=conversation.channel,
                identity=conversation.identity,
                correlation_id=message.correlation_id if message else None,
                payload=payload or {},
            )
        )
