"""Full Steps 1–8 onboarding happy path."""

from __future__ import annotations

from app.shared.workflow import Channel, RunStatus

WA = Channel.WHATSAPP
IDENTITY = "+97455500001"


async def _drive_to_completion(harness):
    runtime = harness.platform.runtime
    start = await runtime.start("onboarding", WA, IDENTITY, input={"trigger": "campaign"})
    assert start.waiting
    assert start.prompt == {"waiting_for": "reply", "step": "campaign"}

    async def resume(message):
        return await runtime.resume(WA, IDENTITY, message=message)

    await resume({"text": "YES"})  # consent + CR ask
    await resume({"attachments": [{"filename": "CR_Company.pdf"}]})  # eligibility + financials ask
    await resume({"attachments": [{"filename": "AuditedReport.pdf"}]})  # pre-qual pending
    await resume({"type": "prequalification", "qualified": True})  # checklist + collect
    await resume(
        {"attachments": [{"filename": "Trade_License.pdf"}, {"filename": "Tax_Card.pdf"}]}
    )  # docs complete + risk
    await resume({"type": "score", "score": 78, "qualified": True})  # payment gate
    await resume({"type": "payment", "paid": True})  # bank submission + lender eval
    await resume({"type": "offers", "offers": [{"offer_id": "o1"}, {"offer_id": "o2"}]})
    return await resume({"type": "offer_selection", "offer_id": "o2"})


async def test_full_onboarding_completes(harness):
    result = await _drive_to_completion(harness)

    assert result.status == RunStatus.COMPLETED
    assert result.values["credit_line_active"] is True
    assert result.values["outcome"] == "completed"
    assert result.values["score"] == 78
    assert result.values["selected_offer"]["offer_id"] == "o2"


async def test_messages_sent_once_in_order(harness):
    await _drive_to_completion(harness)
    templates = harness.messenger.templates()

    # No duplicate sends (the action/await split guarantees this).
    assert len(templates) == len(set(templates))
    assert templates == [
        "onboarding.campaign.intro",
        "onboarding.consent.request",
        "onboarding.financials.request",
        "onboarding.prequal.pending",
        "onboarding.checklist.request",
        "onboarding.documents.complete",
        "onboarding.payment.request",
        "onboarding.submission.confirmed",
        "onboarding.offers.preview",
        "onboarding.creditline.active",
    ]


async def test_external_integrations_invoked(harness):
    await _drive_to_completion(harness)

    assert harness.madad.calls == [
        "check_eligibility",
        "request_prequalification",
        "request_score",
        "submit_to_lenders",
        "activate_credit_line",
    ]
    assert len(harness.payments.links) == 1
    # Reminders scheduled at the wait points and suppressed on action.
    assert "financials_pending" in harness.reminders.scheduled
    assert "incomplete_docs" in harness.reminders.scheduled
    assert "payment_pending" in harness.reminders.scheduled


async def test_locale_propagates(make_harness):
    harness = make_harness()
    runtime = harness.platform.runtime
    await runtime.start("onboarding", WA, IDENTITY, input={"trigger": "campaign", "locale": "ar"})
    # The intro is rendered in Arabic.
    assert harness.messenger.sent[0]["locale"] == "ar"
