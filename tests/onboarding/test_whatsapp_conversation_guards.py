"""WhatsApp conversational guards for the live PDF-style onboarding flow."""

from __future__ import annotations

from app.shared.workflow import Channel


WA = Channel.WHATSAPP
IDENTITY = "+97455500001"
DOC = "ZHVtbXk="


async def test_campaign_question_repeats_yes_no_prompt(harness):
    runtime = harness.platform.runtime
    await runtime.start("onboarding", WA, IDENTITY, input={"trigger": "campaign"})

    result = await runtime.resume(WA, IDENTITY, message={"text": "What is Madad?"})

    assert result.prompt == {"waiting_for": "reply", "step": "campaign"}
    assert "onboarding.help.contextual" in harness.messenger.templates()
    assert "onboarding.campaign.awaiting_yes_no" in harness.messenger.templates()


async def test_upload_steps_require_attachments(make_harness):
    harness = make_harness(known_phones={IDENTITY: "user_42"})
    runtime = harness.platform.runtime
    await runtime.start("onboarding", WA, IDENTITY, input={"trigger": "campaign"})
    await runtime.resume(WA, IDENTITY, message={"text": "YES"})

    result = await runtime.resume(WA, IDENTITY, message={"text": "Am I being scammed?"})

    assert result.prompt == {"waiting_for": "upload", "step": "consent_cr"}
    templates = harness.messenger.templates()
    assert "onboarding.help.contextual" in templates
    assert "onboarding.upload.required" in templates
    assert "onboarding.eligibility.intake.request" not in templates


async def test_filename_only_upload_does_not_advance(make_harness):
    harness = make_harness(known_phones={IDENTITY: "user_42"})
    runtime = harness.platform.runtime
    await runtime.start("onboarding", WA, IDENTITY, input={"trigger": "campaign"})
    await runtime.resume(WA, IDENTITY, message={"text": "YES"})

    result = await runtime.resume(
        WA, IDENTITY, message={"attachments": [{"filename": "CR.pdf"}]}
    )

    assert result.prompt == {"waiting_for": "upload", "step": "consent_cr"}
    templates = harness.messenger.templates()
    assert "onboarding.upload.required" in templates
    assert "onboarding.eligibility.intake.request" not in templates


async def test_documents_text_does_not_complete_kyc(harness):
    runtime = harness.platform.runtime
    await runtime.start("onboarding", WA, IDENTITY, input={"trigger": "campaign"})
    await runtime.resume(WA, IDENTITY, message={"text": "YES"})
    await runtime.resume(WA, IDENTITY, message={"first_name": "Aisha", "last_name": "Karim"})
    await runtime.resume(
        WA, IDENTITY, message={"attachments": [{"filename": "CR.pdf", "content_base64": DOC}]}
    )
    await runtime.resume(WA, IDENTITY, message={"sector": "trade"})
    await runtime.resume(
        WA,
        IDENTITY,
        message={"attachments": [{"filename": "Audited.pdf", "content_base64": DOC}]},
    )
    await runtime.resume(WA, IDENTITY, message={"text": "ACME Trading LLC\nQatar\nops@acme.qa"})
    await runtime.resume(WA, IDENTITY, message={"text": "Aisha Karim 100%"})

    result = await runtime.resume(WA, IDENTITY, message={"text": "Hello"})

    assert result.prompt == {"waiting_for": "upload", "step": "documents"}
    templates = harness.messenger.templates()
    assert "onboarding.upload.required" in templates
    assert "onboarding.documents.complete" not in templates
