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
