"""Template resolution (channel + locale fallback) and the Communication bridge."""

from __future__ import annotations

from app.services.cms import CmsTemplateProvider, build_cms_service
from app.services.communication import (
    OutboundMessageRequest,
    build_communication_service,
)
from app.shared.i18n import Locale
from app.shared.workflow.enums import Channel

WA = Channel.WHATSAPP


async def test_locale_fallback_to_default(cms):
    await cms.upsert_template("welcome", Locale.EN, "Welcome {{ name }}", variables=["name"])
    # No Arabic variant — falls back to English.
    body = await cms.get_template_body("welcome", Locale.AR)
    assert body == "Welcome {{ name }}"


async def test_arabic_variant_preferred_when_present(cms):
    await cms.upsert_template("welcome", Locale.EN, "Welcome")
    await cms.upsert_template("welcome", Locale.AR, "مرحبا")
    assert await cms.get_template_body("welcome", Locale.AR) == "مرحبا"


async def test_channel_specific_template_overrides_agnostic(cms):
    await cms.upsert_template("greeting", Locale.EN, "Generic hi")
    await cms.upsert_template("greeting", Locale.EN, "WhatsApp hi", channel=WA)

    assert await cms.get_template_body("greeting", Locale.EN, channel=WA) == "WhatsApp hi"
    assert await cms.get_template_body("greeting", Locale.EN) == "Generic hi"


async def test_channel_falls_back_to_agnostic(cms):
    await cms.upsert_template("greeting", Locale.EN, "Generic hi")  # no WA variant
    assert await cms.get_template_body("greeting", Locale.EN, channel=WA) == "Generic hi"


async def test_bridge_feeds_communication_service():
    """CMS templates render through the Communication service via the bridge."""

    cms = build_cms_service()
    await cms.upsert_template(
        "welcome", Locale.EN, "Hello {{ name }}, welcome to Madad!", variables=["name"]
    )

    comms = build_communication_service(template_provider=CmsTemplateProvider(cms))
    message = await comms.send(
        OutboundMessageRequest(
            channel=WA,
            identity="+97455500001",
            template_key="welcome",
            variables={"name": "Jathish"},
        )
    )
    assert message.text == "Hello Jathish, welcome to Madad!"


async def test_global_variables_round_trip(cms):
    await cms.set_variables({"company": "Madad", "phone": "72773652"})
    variables = await cms.get_variables()
    assert variables == {"company": "Madad", "phone": "72773652"}


# -- Email subject (UAT 2026-06-28, Ishan #A) ------------------------------


async def test_subject_round_trips_through_cms_and_bridge(cms):
    """Upserting a template with ``subject=...`` stores it on the record
    and the bridge exposes it through ``Template.subject`` so the
    Communication service can thread it into outbound metadata."""

    await cms.upsert_template(
        "onboarding.payment.request",
        Locale.EN,
        "Score: {{ score }} · Pay QAR {{ amount }}",
        subject="Madad — Your Application Result & Next Step",
    )

    bridge = CmsTemplateProvider(cms)
    tpl = await bridge.get("onboarding.payment.request", Locale.EN)
    assert tpl is not None
    assert tpl.subject == "Madad — Your Application Result & Next Step"


async def test_email_in_reply_to_propagates_from_conversation_thread_ref():
    """End-to-end: when the conversation already has a root
    ``external_thread_ref`` (set by an earlier outbound's Message-ID),
    the NEXT email outbound carries it as ``metadata.in_reply_to`` so
    Ishan's email tool can stamp the ``In-Reply-To`` header. WhatsApp
    leaves ``external_thread_ref`` unset, so no header threads through
    on that channel."""

    cms = build_cms_service()
    await cms.upsert_template(
        "onboarding.welcome_back", Locale.EN, "Welcome back!",
        subject="Madad — Welcome Back",
    )
    comms = build_communication_service(template_provider=CmsTemplateProvider(cms))

    # Pre-populate the conversation with a root Message-ID — the
    # situation after the FIRST outbound, where Ishan's tool returned
    # an assigned Message-ID we stored as ``external_thread_ref``.
    convo = await comms._resolve_conversation(  # noqa: SLF001
        Channel.EMAIL, "biz@example.com",
        external_thread_ref="outbound.001@madad",
    )
    convo.external_thread_ref = "outbound.001@madad"
    await comms._conversations.save(convo)  # noqa: SLF001

    message = await comms.send(
        OutboundMessageRequest(
            channel=Channel.EMAIL,
            identity="biz@example.com",
            template_key="onboarding.welcome_back",
            variables={},
        )
    )
    assert message.metadata.get("in_reply_to") == "outbound.001@madad"


async def test_email_in_reply_to_can_be_overridden_by_caller():
    """Caller-supplied ``metadata.in_reply_to`` always wins — even if
    the conversation has a different root Message-ID. Lets the agent
    send a "Re: this one specific email" reply when needed."""
    cms = build_cms_service()
    await cms.upsert_template(
        "onboarding.welcome_back", Locale.EN, "Welcome back!",
        subject="Madad — Welcome Back",
    )
    comms = build_communication_service(template_provider=CmsTemplateProvider(cms))
    convo = await comms._resolve_conversation(  # noqa: SLF001
        Channel.EMAIL, "biz@example.com",
        external_thread_ref="outbound.001@madad",
    )
    convo.external_thread_ref = "outbound.001@madad"
    await comms._conversations.save(convo)  # noqa: SLF001

    message = await comms.send(
        OutboundMessageRequest(
            channel=Channel.EMAIL,
            identity="biz@example.com",
            template_key="onboarding.welcome_back",
            variables={},
            metadata={"in_reply_to": "specific.0099@madad"},
        )
    )
    assert message.metadata.get("in_reply_to") == "specific.0099@madad"


async def test_whatsapp_does_not_pick_up_in_reply_to():
    """Even if the conversation has an ``external_thread_ref`` for
    some weird reason, WhatsApp sends MUST NOT carry ``in_reply_to``
    — the WhatsApp tool would reject the unknown field."""
    cms = build_cms_service()
    await cms.upsert_template(
        "onboarding.welcome_back", Locale.EN, "Welcome back!",
    )
    comms = build_communication_service(template_provider=CmsTemplateProvider(cms))
    convo = await comms._resolve_conversation(  # noqa: SLF001
        Channel.WHATSAPP, "+97455500001",
    )
    convo.external_thread_ref = "outbound.001@madad"
    await comms._conversations.save(convo)  # noqa: SLF001

    message = await comms.send(
        OutboundMessageRequest(
            channel=Channel.WHATSAPP,
            identity="+97455500001",
            template_key="onboarding.welcome_back",
            variables={},
        )
    )
    assert "in_reply_to" not in (message.metadata or {})


async def test_bridge_threads_subject_into_outbound_metadata():
    """End-to-end: an email-channel send rendered through the CMS bridge
    arrives at the gateway with ``metadata.subject`` set. The bridge
    populates Template.subject; the comms service renders it and
    merges into metadata."""

    cms = build_cms_service()
    await cms.upsert_template(
        "onboarding.payment.request",
        Locale.EN,
        "Hi {{ name }}, please complete your QAR 6,000 onboarding fee.",
        subject="Madad — Your Application Result & Next Step",
        variables=["name"],
    )

    comms = build_communication_service(template_provider=CmsTemplateProvider(cms))
    message = await comms.send(
        OutboundMessageRequest(
            channel=Channel.EMAIL,
            identity="biz@example.com",
            template_key="onboarding.payment.request",
            variables={"name": "Jathish"},
        )
    )
    assert message.metadata.get("subject") == "Madad — Your Application Result & Next Step"
    # Body still renders correctly.
    assert "Hi Jathish" in message.text


async def test_explicit_metadata_subject_overrides_template_subject():
    """When the caller passes ``metadata={"subject": "…"}`` explicitly,
    that wins over the CMS-stored subject. Lets a one-off send use a
    custom subject without re-editing the template."""

    cms = build_cms_service()
    await cms.upsert_template(
        "onboarding.welcome_back",
        Locale.EN,
        "Welcome back!",
        subject="Madad — Welcome Back",
    )
    comms = build_communication_service(template_provider=CmsTemplateProvider(cms))

    message = await comms.send(
        OutboundMessageRequest(
            channel=Channel.EMAIL,
            identity="biz@example.com",
            template_key="onboarding.welcome_back",
            variables={},
            metadata={"subject": "Custom subject from the caller"},
        )
    )
    assert message.metadata.get("subject") == "Custom subject from the caller"


async def test_subject_renders_variables_non_strictly():
    """Subject substitutes ``{{ var }}`` from the same bag the body uses.
    Body-strict mode still hard-fails on missing body vars, but a
    missing subject var falls through (gateway defaults absorb it) —
    this prevents a half-rendered "Madad — your {{ amount }} is due"."""

    cms = build_cms_service()
    await cms.upsert_template(
        "test.both",
        Locale.EN,
        "Body uses {{ name }}",
        subject="Hello {{ name }} — amount {{ amount }}",
        variables=["name", "amount"],
    )

    bridge = CmsTemplateProvider(cms)
    tpl = await bridge.get("test.both", Locale.EN)
    assert tpl is not None
    assert tpl.subject == "Hello {{ name }} — amount {{ amount }}"
