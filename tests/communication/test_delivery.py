"""Delivery status tracking + status-transition guards."""

from __future__ import annotations

import pytest

from app.services.communication import (
    CommunicationEventType,
    DeliveryStatusUpdate,
    MessageStatus,
    OutboundMessageRequest,
)
from app.services.communication.errors import (
    InvalidMessageStatusError,
    MessageNotFoundError,
)
from app.shared.workflow import Channel

WA = Channel.WHATSAPP
IDENTITY = "+97455500020"


async def test_delivery_then_read(harness):
    sent = await harness.service.send(
        OutboundMessageRequest(channel=WA, identity=IDENTITY, text="hi")
    )

    delivered = await harness.service.update_delivery_status(
        DeliveryStatusUpdate(
            provider_message_id=sent.provider_message_id, status=MessageStatus.DELIVERED
        )
    )
    assert delivered.status == MessageStatus.DELIVERED
    assert delivered.delivered_at is not None

    read = await harness.service.update_delivery_status(
        DeliveryStatusUpdate(message_id=sent.message_id, status=MessageStatus.READ)
    )
    assert read.status == MessageStatus.READ
    assert read.read_at is not None

    types = harness.event_types()
    assert CommunicationEventType.MESSAGE_DELIVERED in types
    assert CommunicationEventType.MESSAGE_READ in types


async def test_illegal_status_transition_rejected(harness):
    sent = await harness.service.send(
        OutboundMessageRequest(channel=WA, identity=IDENTITY, text="hi")
    )
    await harness.service.update_delivery_status(
        DeliveryStatusUpdate(message_id=sent.message_id, status=MessageStatus.READ)
    )
    # READ is terminal — going back to DELIVERED is illegal.
    with pytest.raises(InvalidMessageStatusError):
        await harness.service.update_delivery_status(
            DeliveryStatusUpdate(message_id=sent.message_id, status=MessageStatus.DELIVERED)
        )


async def test_delivery_for_unknown_message_raises(harness):
    with pytest.raises(MessageNotFoundError):
        await harness.service.update_delivery_status(
            DeliveryStatusUpdate(message_id="msg_nope", status=MessageStatus.DELIVERED)
        )


async def test_conversation_history_orders_messages(harness):
    inbound_conv = await harness.service.resolve_conversation(WA, IDENTITY)
    await harness.service.send(OutboundMessageRequest(channel=WA, identity=IDENTITY, text="one"))
    await harness.service.send(OutboundMessageRequest(channel=WA, identity=IDENTITY, text="two"))

    messages = await harness.service.get_messages(inbound_conv.conversation_id)
    assert [m.text for m in messages] == ["one", "two"]
