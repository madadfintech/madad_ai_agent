"""Full-stack integration: onboarding → real Communication + CMS + Nudge.

Drives the reshaped Phase 2 onboarding workflow with the real Communication
service (rendering CMS templates) and the real Nudge service, proving the
cross-service wiring works end-to-end (not just the in-memory fakes).
"""

from __future__ import annotations

from app.services.cms import CmsTemplateProvider, ConfigKind, build_cms_service
from app.services.communication import build_communication_service
from app.services.nudge import CmsNudgeConfigProvider, build_nudge_service
from app.services.workflow import (
    TEMPLATE_KEYS,
    CommunicationMessenger,
    InMemoryKycClient,
    InMemoryMadadIdentityClient,
    NudgeReminders,
    build_onboarding_platform,
)
from app.shared.i18n import Locale
from app.shared.workflow import Channel, RunStatus

WA = Channel.WHATSAPP
IDENTITY = "+97455500004"


async def test_onboarding_drives_real_communication_and_nudge():
    cms = build_cms_service()
    # Seed all onboarding templates so rendering is deterministic.
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
        identity=InMemoryMadadIdentityClient(journey_status="ELIGIBLE"),
        kyc=InMemoryKycClient(required_documents=["trade_license", "tax_card"]),
        reminders=NudgeReminders(nudge),
    )
    runtime = platform.runtime

    await runtime.start("onboarding", WA, IDENTITY, input={"trigger": "campaign"})

    async def resume(message):
        return await runtime.resume(WA, IDENTITY, message=message)

    doc = "ZHVtbXk="
    await resume({"text": "YES"})
    await resume({"text": "biz@example.com"})  # business_email
    await resume({"attachments": [{"filename": "CR.pdf", "content_base64": doc}]})
    await resume({"attachments": [{"filename": "Audited.pdf", "content_base64": doc}]})
    await resume({"event": "prequalification.completed", "madadScore": 78})
    # Bug #10a + Bug #12 (2026-06-09): one madad_score.ready event exits
    # docs AND fast-forwards through payment_wait into the payment chain.
    await resume(
        {
            "attachments": [
                {"filename": "Establishment_Card.pdf", "content_base64": doc},
            ]
        }
    )
    platform.workflow._identity.journey_status = "QUALIFIED"  # type: ignore[union-attr]
    await resume({"event": "madad_score.ready", "journey_status": "QUALIFIED"})
    await resume({"type": "payment", "paid": True})
    platform.workflow._identity.journey_status = "ACCEPTED"  # type: ignore[union-attr]
    await resume({"type": "status_update"})
    # Ishan 17c3d44 (2026-06-11): run parks after handoff so post-handoff
    # webhooks fire — drive through OFFER_ACCEPTED + ACTIVATED to exercise
    # the full new path. Run stays open at invoice_collect afterwards.
    platform.workflow._identity.journey_status = "OFFER_ACCEPTED"  # type: ignore[union-attr]
    await resume({"type": "status_update", "lenderName": "Qatar Islamic Bank"})
    platform.workflow._identity.journey_status = "ACTIVATED"  # type: ignore[union-attr]
    result = await resume({"type": "status_update", "lenderName": "Qatar Islamic Bank"})

    assert result.status == RunStatus.WAITING_FOR_INPUT
    assert result.prompt == {"waiting_for": "invoice", "step": "invoice_collect"}

    # Communication rendered + recorded real messages from CMS templates.
    conversation = await comms.resolve_conversation(WA, IDENTITY)
    messages = await comms.get_messages(conversation.conversation_id)
    texts = [m.text for m in messages]
    assert "[onboarding.campaign.intro]" in texts
    # WhatsApp uses the button variant (A11); fall back to plain text variant.
    assert any(
        t in texts
        for t in ("[onboarding.offer.handoff.button]", "[onboarding.offer.handoff]")
    )
