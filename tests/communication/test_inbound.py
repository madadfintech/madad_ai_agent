"""Inbound ingestion: persistence, threading, dedupe, attachments, dispatch."""

from __future__ import annotations

from app.services.communication import (
    AttachmentDTO,
    CommunicationEventType,
    InboundMessageDTO,
    MessageDirection,
    MessageStatus,
    MessageType,
)
from app.shared.workflow import Channel

WA = Channel.WHATSAPP
IDENTITY = "+97455500001"


async def test_inbound_persists_and_opens_conversation(harness):
    dto = InboundMessageDTO(channel=WA, identity=IDENTITY, text="YES")
    message = await harness.service.ingest_inbound(dto)

    assert message.direction == MessageDirection.INBOUND
    assert message.status == MessageStatus.RECEIVED
    assert message.text == "YES"

    conversation = await harness.service.resolve_conversation(WA, IDENTITY)
    assert conversation.conversation_id == message.conversation_id
    assert conversation.message_count == 1

    types = harness.event_types()
    assert CommunicationEventType.CONVERSATION_OPENED in types
    assert CommunicationEventType.MESSAGE_RECEIVED in types


async def test_two_inbounds_share_one_conversation(harness):
    first = await harness.service.ingest_inbound(
        InboundMessageDTO(channel=WA, identity=IDENTITY, text="hi")
    )
    second = await harness.service.ingest_inbound(
        InboundMessageDTO(channel=WA, identity=IDENTITY, text="again")
    )
    assert first.conversation_id == second.conversation_id


async def test_email_and_whatsapp_are_separate_conversations(harness):
    wa = await harness.service.ingest_inbound(
        InboundMessageDTO(channel=WA, identity=IDENTITY, text="hi")
    )
    email = await harness.service.ingest_inbound(
        InboundMessageDTO(channel=Channel.EMAIL, identity="sme@example.com", text="hi")
    )
    assert wa.conversation_id != email.conversation_id


async def test_inbound_dedupe_on_provider_id(harness):
    dto = InboundMessageDTO(
        channel=WA, identity=IDENTITY, text="YES", provider_message_id="wamid.123"
    )
    first = await harness.service.ingest_inbound(dto)
    second = await harness.service.ingest_inbound(dto)
    assert first.message_id == second.message_id

    messages = await harness.service.get_messages(first.conversation_id)
    assert len(messages) == 1


async def test_inbound_attachment_metadata_and_event(harness):
    dto = InboundMessageDTO(
        channel=WA,
        identity=IDENTITY,
        type=MessageType.MEDIA,
        attachments=[
            AttachmentDTO(
                filename="CR_Company.pdf", content_type="application/pdf", provider_ref="media-1"
            )
        ],
    )
    message = await harness.service.ingest_inbound(dto)

    assert len(message.attachments) == 1
    assert message.attachments[0].filename == "CR_Company.pdf"
    assert message.attachments[0].direction == MessageDirection.INBOUND
    assert CommunicationEventType.ATTACHMENT_RECEIVED in harness.event_types()


async def test_inbound_hands_off_to_dispatcher(make_harness):
    received: list[str] = []

    class RecordingDispatcher:
        async def on_inbound(self, message) -> None:
            received.append(message.message_id)

    from app.services.communication import build_communication_service

    service = build_communication_service(dispatcher=RecordingDispatcher())
    message = await service.ingest_inbound(
        InboundMessageDTO(channel=WA, identity=IDENTITY, text="YES")
    )
    assert received == [message.message_id]
