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
