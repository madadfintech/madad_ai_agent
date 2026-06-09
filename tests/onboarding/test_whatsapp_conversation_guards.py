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
    uploads), using the live PDF flow: YES → CR → audited financials → account
    created (PARK) → pre-qualification trigger → document checklist.

    No form-filling and no eligibility questionnaire — both removed per the PDF.
    The pre-qualification is released by an external trigger (Postman in the
    demo), simulated here with a prequalification.completed resume payload.
    """

    await runtime.start("onboarding", WA, IDENTITY, input={"trigger": "campaign"})
    await runtime.resume(WA, IDENTITY, message={"text": "YES"})
    # CR upload → financials request.
    await runtime.resume(
        WA, IDENTITY, message={"attachments": [{"filename": "CR.pdf", "content_base64": DOC}]}
    )
    # Audited financials → account-created → PARK at the pre-qualification gate.
    await runtime.resume(
        WA,
        IDENTITY,
        message={"attachments": [{"filename": "Audited.pdf", "content_base64": DOC}]},
    )
    # Postman pre-qualification trigger → document checklist.
    return await runtime.resume(
        WA,
        IDENTITY,
        message={"event": "prequalification.completed", "journey_status": "PRE_QUALIFIED"},
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


async def test_single_document_upload_parks_in_loop(harness):
    """Bug #10a (2026-06-09): a single (typically misclassified) upload no
    longer completes the docs loop. QA saw "🎊 all documents received"
    fire after one passport upload that the backend tagged as CR — the
    "lenient" route is gone; the loop now stays parked until the asked-
    for checklist is exhausted OR a forward-status webhook lands."""

    runtime = harness.platform.runtime
    await _drive_to_documents(harness, runtime)

    result = await runtime.resume(
        WA,
        IDENTITY,
        message={"attachments": [{"filename": "IMG-001.jpg", "content_base64": DOC}]},
    )

    assert result.prompt == {"waiting_for": "upload", "step": "documents"}
    templates = harness.messenger.templates()
    assert "onboarding.documents.complete" not in templates


async def test_cr_upload_asks_for_financials_not_questionnaire(harness):
    """After the CR the agent asks for the audited financials directly — the
    eligibility questionnaire is not part of the PDF flow."""

    runtime = harness.platform.runtime
    await runtime.start("onboarding", WA, IDENTITY, input={"trigger": "campaign"})
    await runtime.resume(WA, IDENTITY, message={"text": "YES"})

    result = await runtime.resume(
        WA, IDENTITY, message={"attachments": [{"filename": "CR.pdf", "content_base64": DOC}]}
    )

    assert result.prompt == {"waiting_for": "upload", "step": "financials"}
    templates = harness.messenger.templates()
    assert "onboarding.financials.request" in templates
    assert "onboarding.eligibility.intake.request" not in templates


async def test_qualify_mid_docs_fast_forwards_through_payment_wait(harness):
    """Bug #12 (UAT 2026-06-09, Ishan diagnosis): backend only fires
    ``madad_score.ready`` once. When it arrives mid-docs-loop, the SAME
    event must:
      (a) exit the docs loop (Bug #10a escape hatch),
      (b) trigger the payment chain on the same resume — not park at
          payment_wait waiting for a second event that won't come.

    Without (b), the run sat at payment_wait until the admin re-fired
    qualify, by which point the access token was stale → 401 → run
    failed → no payment link reached the SME."""

    runtime = harness.platform.runtime
    await _drive_to_documents(harness, runtime)

    # One ``madad_score.ready`` event (mapped to QUALIFIED by the
    # dispatcher's translator) must produce the payment template in
    # the SAME resume — no second trigger needed.
    await runtime.resume(
        WA,
        IDENTITY,
        message={
            "event": "madad_score.ready",
            "journey_status": "QUALIFIED",
            "madadScore": 78,
        },
    )

    templates = harness.messenger.templates()
    # Either the interactive CTA-URL button or the plain-text payment
    # template fired — both are valid end-states for the chain.
    assert (
        "onboarding.payment.request.button" in templates
        or "onboarding.payment.request" in templates
    )
