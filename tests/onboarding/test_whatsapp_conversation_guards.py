"""WhatsApp conversational guards for the live PDF-style onboarding flow.

The live flow (per the onboarding PDF) goes:
campaign → details → consent/CR → eligibility → audited financials →
documents → coffee. Buyer + shareholder "ask" steps are intentionally NOT in
the graph. The documents step is lenient and conversational: a question or a
"no" is answered in context (``onboarding.help.contextual``) — never the old
robotic "upload required" nag — and any single uploaded file completes it.
"""

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
    assert "onboarding.upload.required" not in templates
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


async def _drive_to_documents(harness, runtime):
    """Advance a fresh WhatsApp run to the documents step (parked awaiting
    uploads), using the live graph (no buyer/shareholder steps)."""

    await runtime.start("onboarding", WA, IDENTITY, input={"trigger": "campaign"})
    await runtime.resume(WA, IDENTITY, message={"text": "YES"})
    await runtime.resume(WA, IDENTITY, message={"first_name": "Aisha", "last_name": "Karim"})
    await runtime.resume(
        WA, IDENTITY, message={"attachments": [{"filename": "CR.pdf", "content_base64": DOC}]}
    )
    await runtime.resume(WA, IDENTITY, message={"sector": "trade"})
    return await runtime.resume(
        WA,
        IDENTITY,
        message={"attachments": [{"filename": "Audited.pdf", "content_base64": DOC}]},
    )


async def test_documents_text_does_not_complete_kyc(harness):
    runtime = harness.platform.runtime
    await _drive_to_documents(harness, runtime)

    # A plain-text reply at the documents step must NOT be treated as an
    # upload and must NOT complete KYC — and must NOT fire the old robotic
    # "upload required" nag. It is answered conversationally instead.
    result = await runtime.resume(WA, IDENTITY, message={"text": "Hello"})

    assert result.prompt == {"waiting_for": "upload", "step": "documents"}
    templates = harness.messenger.templates()
    assert "onboarding.help.contextual" in templates
    assert "onboarding.upload.required" not in templates
    assert "onboarding.documents.complete" not in templates


async def test_document_status_query_does_not_complete_or_reprompt_missing(harness):
    runtime = harness.platform.runtime
    await _drive_to_documents(harness, runtime)

    result = await runtime.resume(
        WA, IDENTITY, message={"text": "What's my application status?"}
    )

    assert result.prompt == {"waiting_for": "upload", "step": "documents"}
    templates = harness.messenger.templates()
    assert "onboarding.help.contextual" in templates
    assert "onboarding.documents.complete" not in templates


async def test_single_document_upload_completes_lenient(harness):
    runtime = harness.platform.runtime
    await _drive_to_documents(harness, runtime)

    # Lenient: one uploaded file completes the document phase and sends the
    # "documents complete" (coffee) message rather than re-listing what's left.
    await runtime.resume(
        WA,
        IDENTITY,
        message={"attachments": [{"filename": "IMG-001.jpg", "content_base64": DOC}]},
    )

    templates = harness.messenger.templates()
    assert "onboarding.documents.complete" in templates


async def test_negative_reply_does_not_submit_eligibility(make_harness):
    harness = make_harness(known_phones={IDENTITY: "user_42"})
    runtime = harness.platform.runtime
    await runtime.start("onboarding", WA, IDENTITY, input={"trigger": "campaign"})
    await runtime.resume(WA, IDENTITY, message={"text": "YES"})
    await runtime.resume(
        WA, IDENTITY, message={"attachments": [{"filename": "CR.pdf", "content_base64": DOC}]}
    )

    result = await runtime.resume(WA, IDENTITY, message={"text": "No"})

    assert result.prompt == {"waiting_for": "eligibility_form", "step": "eligibility"}
    templates = harness.messenger.templates()
    assert "onboarding.financials.request" not in templates
