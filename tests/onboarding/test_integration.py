"""Full-stack integration: onboarding → real Communication + CMS + Nudge.

Drives the onboarding workflow with the real Communication service (rendering CMS
templates) and the real Nudge service, proving the cross-service wiring works
end-to-end (not just the in-memory fakes).
"""

from __future__ import annotations

from app.services.cms import CmsTemplateProvider, ConfigKind, build_cms_service
from app.services.communication import build_communication_service
from app.services.nudge import CmsNudgeConfigProvider, build_nudge_service
from app.services.workflow import (
    TEMPLATE_KEYS,
    CommunicationMessenger,
    InMemoryDocumentIntake,
    InMemoryMadadClient,
    InMemoryPaymentClient,
    NudgeReminders,
    build_onboarding_platform,
)
from app.shared.i18n import Locale
from app.shared.workflow import Channel, RunStatus

WA = Channel.WHATSAPP
IDENTITY = "+97455500004"


async def test_onboarding_drives_real_communication_and_nudge():
    cms = build_cms_service()
    # Seed all onboarding templates (plain bodies so rendering is deterministic).
    for key in TEMPLATE_KEYS:
        await cms.upsert_template(key, Locale.EN, f"[{key}]")
    # Seed a nudge schedule so the real Nudge service accepts the scheduling.
    await cms.upsert(
        ConfigKind.NUDGE,
        "incomplete_docs",
        {"schedule": [{"offset": 0, "channels": ["whatsapp"], "template_key": "nudge.docs.1"}]},
    )

    comms = build_communication_service(template_provider=CmsTemplateProvider(cms))
    nudge = build_nudge_service(config_provider=CmsNudgeConfigProvider(cms))

    platform = build_onboarding_platform(
        messenger=CommunicationMessenger(comms),
        documents=InMemoryDocumentIntake(
            required=["trade_license", "tax_card"],
            type_by_keyword={"trade": "trade_license", "tax": "tax_card"},
        ),
        madad=InMemoryMadadClient(),
        payments=InMemoryPaymentClient(),
        reminders=NudgeReminders(nudge),
    )
    runtime = platform.runtime

    await runtime.start("onboarding", WA, IDENTITY, input={"trigger": "campaign"})

    async def resume(message):
        return await runtime.resume(WA, IDENTITY, message=message)

    await resume({"text": "YES"})
    await resume({"attachments": [{"filename": "CR.pdf"}]})
    await resume({"attachments": [{"filename": "Audited.pdf"}]})
    await resume({"type": "prequalification", "qualified": True})
    await resume({"attachments": [{"filename": "Trade_License.pdf"}, {"filename": "Tax_Card.pdf"}]})
    await resume({"type": "score", "score": 78, "qualified": True})
    await resume({"type": "payment", "paid": True})
    await resume({"type": "offers", "offers": [{"offer_id": "o1"}]})
    result = await resume({"type": "offer_selection", "offer_id": "o1"})

    assert result.status == RunStatus.COMPLETED

    # Communication rendered + recorded real messages from CMS templates.
    conversation = await comms.resolve_conversation(WA, IDENTITY)
    messages = await comms.get_messages(conversation.conversation_id)
    texts = [m.text for m in messages]
    assert "[onboarding.campaign.intro]" in texts
    assert "[onboarding.creditline.active]" in texts
