"""Full Steps 1–8 onboarding happy path against the MCP-shaped graph."""

from __future__ import annotations

from app.shared.workflow import Channel, RunStatus

WA = Channel.WHATSAPP
IDENTITY = "+97455500001"


async def _drive_to_completion(harness):
    """Drive the new-lead happy path from campaign through offer handoff.

    Mutates ``harness.identity.journey_status`` between turns to model the
    backend advancing through ELIGIBLE → PRE_QUALIFIED → QUALIFIED → ACCEPTED.
    """

    runtime = harness.platform.runtime
    async def resume(message):
        return await runtime.resume(WA, IDENTITY, message=message)

    start = await runtime.start("onboarding", WA, IDENTITY, input={"trigger": "campaign"})
    assert start.waiting
    assert start.prompt == {"waiting_for": "reply", "step": "campaign"}

    # YES → check_contact runs as a passthrough; falls through to the new-lead
    # branch (phone not in known_phones) → onboarding details prompt.
    after_yes = await resume({"text": "YES"})
    assert after_yes.prompt == {"waiting_for": "reply", "step": "collect_details"}

    # First name + last name → complete_onboarding → second session → consent.
    after_name = await resume({"first_name": "Aisha", "last_name": "Karim"})
    assert after_name.prompt == {"waiting_for": "upload", "step": "consent_cr"}

    # CR upload → eligibility intake prompt.
    after_cr = await resume({"attachments": [{"filename": "CR.pdf"}]})
    assert after_cr.prompt == {"waiting_for": "eligibility_form", "step": "eligibility"}

    # Eligibility form → KYC update returns eligible (InMemory default).
    # Routes to financials_send.
    after_form = await resume({"annual_revenue_qar": 5_000_000, "sector": "trade"})
    assert after_form.prompt == {"waiting_for": "upload", "step": "financials"}

    # Audited report → list-fetch → buyers prompt.
    after_fin = await resume({"attachments": [{"filename": "Audited.pdf"}]})
    assert after_fin.prompt == {"waiting_for": "buyers", "step": "buyers"}

    # Buyer info → shareholders prompt.
    after_buyer = await resume({"name": "ACME LLC", "country": "QA"})
    assert after_buyer.prompt == {"waiting_for": "shareholders", "step": "shareholders"}

    # Shareholders → documents upload prompt.
    after_sh = await resume(
        {"shareholders": [{"name": "Aisha", "percentage": 100}]}
    )
    assert after_sh.prompt == {"waiting_for": "upload", "step": "documents"}

    # Upload both required docs in one turn → documents_complete →
    # status_poll_on_demand (journey still ELIGIBLE) → journey_wait_await.
    after_docs = await resume(
        {
            "attachments": [
                {"filename": "Trade_License.pdf"},
                {"filename": "Tax_Card.pdf"},
            ]
        }
    )
    assert after_docs.prompt == {
        "waiting_for": "journey_status",
        "step": "journey_wait",
    }

    # Advance backend → PRE_QUALIFIED, then resume from wait. Re-poll sees
    # PRE_QUALIFIED → payment_send → payment_await.
    harness.identity.journey_status = "PRE_QUALIFIED"
    after_status1 = await resume({"type": "status_update"})
    assert after_status1.prompt == {"waiting_for": "payment", "step": "payment"}

    # Mark monetization fee paid → lender_status_poll (still PRE_QUALIFIED) →
    # lender_wait_await.
    after_pay = await resume({"type": "payment", "paid": True})
    assert after_pay.prompt == {
        "waiting_for": "journey_status",
        "step": "lender_wait",
    }

    # Backend advances → ACCEPTED → offers_fetch → offer_view → handoff.
    harness.identity.journey_status = "ACCEPTED"
    return await resume({"type": "status_update"})


async def test_full_onboarding_completes(harness):
    result = await _drive_to_completion(harness)

    assert result.status == RunStatus.COMPLETED
    assert result.values["outcome"] == "offer_handoff"
    assert result.values["onboarding_first_name"] == "Aisha"
    assert result.values["onboarding_last_name"] == "Karim"
    assert result.values["consent"] is True
    assert result.values["cr_ref"] == "CR.pdf"
    assert result.values["eligible"] is True
    assert result.values["financials_received"] is True
    assert result.values["paid"] is True
    assert result.values["missing_documents"] == []


async def test_messages_sent_once_in_order(harness):
    await _drive_to_completion(harness)
    templates = harness.messenger.templates()

    # No duplicate sends (the action/await split guarantees this).
    assert len(templates) == len(set(templates))
    assert templates == [
        "onboarding.campaign.intro",
        "onboarding.collect_details.request",
        "onboarding.consent.request",
        "onboarding.eligibility.intake.request",
        "onboarding.financials.request",
        "onboarding.buyers.request",
        "onboarding.shareholders.request",
        "onboarding.documents.checklist",
        "onboarding.documents.complete",
        "onboarding.payment.request",
        "onboarding.offers.preview",
        "onboarding.offer.handoff",
    ]


async def test_mcp_tool_calls_happen_in_order(harness):
    await _drive_to_completion(harness)

    identity_calls = [name for name, _ in harness.identity.calls]
    kyc_calls = [name for name, _ in harness.kyc.calls]

    # Identity: check_contact (Q8) → open_session (new lead) → complete →
    # open_session (second, post-promotion) → me (Step-7 poll x2) →
    # me (Step-8 poll x2) → me (offers_fetch).
    assert identity_calls == [
        "check_contact",
        "open_session",
        "complete_onboarding",
        "open_session",
        "me",
        "me",
        "me",
        "me",
        "me",
    ]

    # KYC: CR upload → eligibility intake → financial report → list →
    # buyer → shareholders → list → doc upload x2 → list.
    assert kyc_calls == [
        "upload_commercial_registration",
        "update_eligibility",
        "upload_audited_financial_report",
        "get_admin_requested_documents",
        "add_buyer",
        "add_shareholders",
        "upload_document_base64",
        "upload_document_base64",
        "get_admin_requested_documents",
    ]


async def test_reminders_scheduled_and_suppressed_at_wait_points(harness):
    await _drive_to_completion(harness)

    # Reminders fire at every send-and-wait point and are suppressed on
    # reply / payment / docs-complete.
    assert "eligibility_pending" in harness.reminders.scheduled
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
    assert after_yes.prompt == {"waiting_for": "upload", "step": "consent_cr"}

    identity_calls = [name for name, _ in harness.identity.calls]
    # check_contact + open_session (single bridge call for existing user, no
    # complete_onboarding, no second session).
    assert identity_calls == ["check_contact", "open_session"]


async def test_locale_propagates(make_harness):
    harness = make_harness()
    runtime = harness.platform.runtime
    await runtime.start("onboarding", WA, IDENTITY, input={"trigger": "campaign", "locale": "ar"})
    assert harness.messenger.sent[0]["locale"] == "ar"
