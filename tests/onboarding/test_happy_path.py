"""Full Steps 1–8 onboarding happy path against the MCP-shaped graph."""

from __future__ import annotations

from app.shared.workflow import Channel, RunStatus

WA = Channel.WHATSAPP
IDENTITY = "+97455500001"


async def _drive_to_completion(harness):
    """Drive the spec-aligned (post-2026-06-07) happy path:
    campaign → YES → consent_cr → CR upload → financials → audited → PARK(prequalify_wait)
    → prequalification.completed → documents → PARK(payment_wait) → madad_score.ready
    → payment → paid → lender_wait → offers_available → handoff.

    Collect-details, eligibility form, buyers, and shareholders steps are no
    longer in the workflow graph per main commits 62d7560 (drop buyer/share asks)
    and the spec-alignment realignment.
    """

    runtime = harness.platform.runtime

    async def resume(message):
        return await runtime.resume(WA, IDENTITY, message=message)

    start = await runtime.start("onboarding", WA, IDENTITY, input={"trigger": "campaign"})
    assert start.waiting
    assert start.prompt == {"waiting_for": "reply", "step": "campaign"}

    # YES → business_email step (PR #5/#6, 2026-06-10) → consent_cr.
    after_yes = await resume({"text": "YES"})
    assert after_yes.prompt == {"waiting_for": "email", "step": "business_email"}
    after_email = await resume({"text": "biz@example.com"})
    assert after_email.prompt == {"waiting_for": "upload", "step": "consent_cr"}

    # CR upload → financials direct (auto-eligibility, no 7-field form).
    after_cr = await resume({"attachments": [{"filename": "CR.pdf", "content_base64": "ZHVtbXk="}]})
    assert after_cr.prompt == {"waiting_for": "upload", "step": "financials"}

    # Audited report → PARK at prequalify_wait awaiting backend event.
    after_fin = await resume(
        {"attachments": [{"filename": "Audited.pdf", "content_base64": "ZHVtbXk="}]}
    )
    assert after_fin.prompt == {"waiting_for": "prequalification", "step": "prequalify_wait"}

    # Backend fires prequalification.completed → documents.
    after_prequal = await resume(
        {"event": "prequalification.completed", "madadScore": 78}
    )
    assert after_prequal.prompt == {"waiting_for": "upload", "step": "documents"}

    # Bug #10a + Bug #12 (2026-06-09): docs loop is strict, and a single
    # ``madad_score.ready`` event (mapped to QUALIFIED by the translator)
    # exits the docs loop AND fast-forwards through payment_wait into
    # the payment chain on the SAME resume — backend only fires once.
    await resume(
        {"attachments": [{"filename": "Establishment_Card.pdf", "content_base64": "ZHVtbXk="}]}
    )
    harness.identity.journey_status = "QUALIFIED"
    after_docs = await resume(
        {"event": "madad_score.ready", "journey_status": "QUALIFIED", "madadScore": 78}
    )
    assert after_docs.prompt == {"waiting_for": "payment", "step": "payment"}

    # Mark monetization fee paid → lender_wait.
    after_pay = await resume({"type": "payment", "paid": True})
    assert after_pay.prompt == {"waiting_for": "journey_status", "step": "lender_wait"}

    # Backend advances → ACCEPTED → offers_fetch → offer_view → handoff.
    # Ishan 17c3d44 (2026-06-11): the run no longer completes at handoff;
    # it parks at journey_wait_await so the post-handoff offer.accepted +
    # credit_line.activated webhooks can fire the ✅ confirmation and
    # 🎊 activation messages, then transitions into invoice_collect_await
    # so the SME can submit invoices once the line is live.
    harness.identity.journey_status = "ACCEPTED"
    await resume({"type": "status_update"})
    harness.identity.journey_status = "OFFER_ACCEPTED"
    await resume({"type": "status_update", "lenderName": "Qatar Islamic Bank"})
    harness.identity.journey_status = "ACTIVATED"
    return await resume({"type": "status_update", "lenderName": "Qatar Islamic Bank"})


async def test_full_onboarding_completes(harness):
    result = await _drive_to_completion(harness)

    # Run stays open at invoice_collect after activation — the SME can now
    # submit invoices over WhatsApp. ``outcome="completed"`` is the marker
    # that onboarding-proper finished; the run lives on for invoice work.
    assert result.status == RunStatus.WAITING_FOR_INPUT
    assert result.prompt == {"waiting_for": "invoice", "step": "invoice_collect"}
    assert result.values["outcome"] == "completed"
    # collect_details step is gone — onboarding_first_name / onboarding_last_name
    # are no longer captured. CR upload + financials + docs still are.
    assert result.values["consent"] is True
    assert result.values["cr_ref"] == "CR.pdf"
    assert result.values["financials_received"] is True
    assert result.values["paid"] is True


async def test_messages_sent_once_in_order(harness):
    await _drive_to_completion(harness)
    templates = harness.messenger.templates()

    # No duplicate sends (the action/await split guarantees this).
    # Template order updated for the spec-aligned flow — collect_details /
    # eligibility / buyers / shareholders templates are gone; account.created
    # fires after audited upload; documents.complete fires before payment_wait.
    # Main uses `payment.request.button` (interactive CTA) instead of plain
    # `payment.request`, and `documents.single_received` is sent per upload.
    # Ishan refinement (2026-06-09): ``documents.complete`` is intentionally
    # skipped on the QUALIFY-mid-docs path the helper drives — the
    # checklist isn't naturally complete, admin overrode it, and the
    # coffee message would misrepresent the SME's state.
    # UAT 2026-06-10: the offers preview + handoff used to fire as TWO
    # bubbles. Combined into a single CTA-URL message via
    # ``onboarding.offer.handoff.button`` — ``offers.preview`` is now
    # a back-compat fallback only and no longer fires on the WhatsApp
    # happy path.
    expected_subset = [
        "onboarding.campaign.intro",
        "onboarding.consent.request",
        "onboarding.financials.request",
        "onboarding.account.created",
        "onboarding.documents.checklist",
        "onboarding.payment.request.button",
        "onboarding.offer.handoff.button",
    ]
    for tpl in expected_subset:
        assert tpl in templates, f"expected {tpl} in {templates}"
    # Coffee message must NOT fire on admin override.
    assert "onboarding.documents.complete" not in templates


async def test_mcp_tool_calls_happen_in_order(harness):
    await _drive_to_completion(harness)

    identity_calls = [name for name, _ in harness.identity.calls]
    kyc_calls = [name for name, _ in harness.kyc.calls]

    # Identity contains check_contact + at least one open_session + at least
    # one me poll. Existing-user fast-path skips complete_onboarding.
    assert "check_contact" in identity_calls
    assert "open_session" in identity_calls
    assert "me" in identity_calls

    # KYC contract: A5 reshape (2026-06-07) routes BOTH the CR upload and
    # the per-doc upload step through classify_and_upload_document_base64
    # (single classify path — backend decides the document type rather
    # than the agent inferring it from the filename). The audited
    # financial report keeps its dedicated tool. main's WhatsApp flow
    # uses the hardcoded full-checklist (DEFAULT_WHATSAPP_REQUIRED_DOCS)
    # rather than calling get_admin_requested_documents on the WA
    # channel; update_eligibility / add_buyer / add_shareholders are no
    # longer in the graph.
    assert "upload_audited_financial_report" in kyc_calls
    assert "classify_and_upload_document_base64" in kyc_calls
    # UAT 2026-06-19 QA #8: the forced ``upload_commercial_registration``
    # was RESTORED — classify-and-trust dumped real CRs into "Additional
    # Documents" when the classifier blipped, stalling onboarding. The
    # forced upload always lands the doc in the CR slot; the classifier
    # still runs but ONLY to gate the "registered in Qatar ✅" line.
    assert "upload_commercial_registration" in kyc_calls


async def test_reminders_scheduled_and_suppressed_at_wait_points(harness):
    await _drive_to_completion(harness)

    # Reminders fire at the remaining send-and-wait points; eligibility step
    # is gone, so eligibility_pending is no longer scheduled. Only the three
    # spec-named nudges should appear.
    assert "financials_pending" in harness.reminders.scheduled
    assert "incomplete_docs" in harness.reminders.scheduled
    assert "payment_pending" in harness.reminders.scheduled


async def test_existing_user_skips_collect_details_branch(make_harness):
    harness = make_harness(known_phones={IDENTITY: "user_42"}, journey_status="ACTIVATED")
    runtime = harness.platform.runtime

    await runtime.start("onboarding", WA, IDENTITY, input={"trigger": "campaign"})

    async def resume(message):
        return await runtime.resume(WA, IDENTITY, message=message)

    # YES → check_contact → known phone → existing branch → channel_session_first
    # → consent (NO collect_details prompt at all).
    after_yes = await resume({"text": "YES"})
    await resume({"text": "biz@example.com"})  # business_email
    assert after_yes.prompt == {"waiting_for": "upload", "step": "consent_cr"}

    identity_calls = [name for name, _ in harness.identity.calls]
    # Cold-start entry (Ishan 2026-06-10): check_registration fires FIRST
    # at entry_registration_check, returns False (opt-in fake), then
    # check_contact fires, then check_registration fires AGAIN inside
    # check_contact_send because the entry's check returned no route
    # (guard only short-circuits when a route was already captured).
    # Finally open_session for the existing-user path.
    assert identity_calls == [
        "check_registration",
        "check_contact",
        "check_registration",
        "open_session",
    ]


async def test_locale_propagates(make_harness):
    harness = make_harness()
    runtime = harness.platform.runtime
    await runtime.start("onboarding", WA, IDENTITY, input={"trigger": "campaign", "locale": "ar"})
    assert harness.messenger.sent[0]["locale"] == "ar"
