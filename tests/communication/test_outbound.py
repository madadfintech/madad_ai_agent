"""Outbound send: dispatch, templating, multilingual, retry, failure."""

from __future__ import annotations

import pytest

from app.services.communication import (
    CommunicationConfig,
    CommunicationEventType,
    Locale,
    MessageDirection,
    MessageStatus,
    MessageType,
    OutboundMessageRequest,
)
from app.services.communication.errors import MissingTemplateVariableError
from app.shared.workflow import Channel

WA = Channel.WHATSAPP
IDENTITY = "+97455500010"


async def test_send_text_dispatches_and_marks_sent(harness):
    message = await harness.service.send(
        OutboundMessageRequest(channel=WA, identity=IDENTITY, text="Hello!")
    )

    assert message.direction == MessageDirection.OUTBOUND
    assert message.status == MessageStatus.SENT
    assert message.provider_message_id is not None
    assert len(harness.gateway.sent) == 1

    types = harness.event_types()
    assert CommunicationEventType.MESSAGE_QUEUED in types
    assert CommunicationEventType.MESSAGE_SENT in types


async def test_send_template_renders_variables(harness):
    harness.templates.add("welcome", Locale.EN, "Hello {{ name }}, welcome to Madad!")
    message = await harness.service.send(
        OutboundMessageRequest(
            channel=WA,
            identity=IDENTITY,
            template_key="welcome",
            variables={"name": "Jathish"},
        )
    )
    assert message.type == MessageType.TEMPLATE
    assert message.text == "Hello Jathish, welcome to Madad!"


async def test_template_missing_variable_raises(harness):
    harness.templates.add("welcome", Locale.EN, "Hello {{ name }}")
    with pytest.raises(MissingTemplateVariableError):
        await harness.service.send(
            OutboundMessageRequest(channel=WA, identity=IDENTITY, template_key="welcome")
        )


async def test_arabic_locale_uses_arabic_template(harness):
    harness.templates.add("welcome", Locale.EN, "Welcome {{ name }}")
    harness.templates.add("welcome", Locale.AR, "مرحبا {{ name }}")
    message = await harness.service.send(
        OutboundMessageRequest(
            channel=WA,
            identity=IDENTITY,
            template_key="welcome",
            variables={"name": "Ali"},
            locale=Locale.AR,
        )
    )
    assert message.text == "مرحبا Ali"
    assert message.locale == Locale.AR


async def test_locale_falls_back_to_default_when_missing(harness):
    harness.templates.add("welcome", Locale.EN, "Welcome {{ name }}")  # no AR variant
    message = await harness.service.send(
        OutboundMessageRequest(
            channel=WA,
            identity=IDENTITY,
            template_key="welcome",
            variables={"name": "Ali"},
            locale=Locale.AR,
        )
    )
    assert message.text == "Welcome Ali"  # fell back to EN body


async def test_transient_failure_retried_then_sent(make_harness):
    harness = make_harness(fail_times=2)
    message = await harness.service.send(
        OutboundMessageRequest(channel=WA, identity=IDENTITY, text="hi")
    )
    assert message.status == MessageStatus.SENT
    assert message.attempts == 3
    retrying = [t for t in harness.event_types() if t == CommunicationEventType.MESSAGE_RETRYING]
    assert len(retrying) == 2


async def test_retry_exhaustion_marks_failed(make_harness):
    harness = make_harness(
        fail_times=10,
        config=CommunicationConfig(retry_max_attempts=2, retry_base_delay=0.0, retry_jitter=False),
    )
    message = await harness.service.send(
        OutboundMessageRequest(channel=WA, identity=IDENTITY, text="hi")
    )
    assert message.status == MessageStatus.FAILED
    assert message.last_error is not None
    assert CommunicationEventType.MESSAGE_FAILED in harness.event_types()


async def test_email_send_rotates_conversation_thread_ref(harness):
    """After a successful email send, ``conversation.external_thread_ref`` is
    set to the send's ``provider_message_id`` so the NEXT agent-initiated
    email inherits ``in_reply_to`` and stays visually stitched in the SME's
    inbox. WhatsApp deliberately does NOT rotate — threads by identity."""

    first = await harness.service.send(
        OutboundMessageRequest(
            channel=Channel.EMAIL, identity="sme@example.qa", text="Hi"
        )
    )
    assert first.status == MessageStatus.SENT
    assert first.provider_message_id is not None

    conversation = await harness.service._conversations.get(first.conversation_id)
    assert conversation.external_thread_ref == first.provider_message_id

    second = await harness.service.send(
        OutboundMessageRequest(
            channel=Channel.EMAIL, identity="sme@example.qa", text="Follow-up"
        )
    )
    assert second.provider_message_id != first.provider_message_id

    # Gateway saw the second send carry in_reply_to = first send's id.
    second_call = harness.gateway.sent[-1]
    assert second_call.metadata is not None
    assert second_call.metadata.get("in_reply_to") == first.provider_message_id

    # And the conversation now points at the LATEST send.
    conversation = await harness.service._conversations.get(first.conversation_id)
    assert conversation.external_thread_ref == second.provider_message_id


async def test_whatsapp_send_does_not_rotate_thread_ref(harness):
    """WhatsApp does not thread by message-id; ``external_thread_ref`` stays
    None (the initial conversation state) after send."""

    message = await harness.service.send(
        OutboundMessageRequest(channel=WA, identity=IDENTITY, text="hi")
    )
    assert message.status == MessageStatus.SENT
    conversation = await harness.service._conversations.get(message.conversation_id)
    assert conversation.external_thread_ref is None
