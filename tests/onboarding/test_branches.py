"""Onboarding decision branches: decline, domain-blocked, ineligible,
missing-docs loop, unqualified post-payment."""

from __future__ import annotations

from app.shared.workflow import Channel, RunStatus

WA = Channel.WHATSAPP
EMAIL = Channel.EMAIL
IDENTITY = "+97455500002"
EMAIL_ID = "newhire@blocked.qa"


async def test_decline_at_campaign_entry(harness):
    runtime = harness.platform.runtime
    await runtime.start("onboarding", WA, IDENTITY, input={"trigger": "campaign"})
    result = await runtime.resume(WA, IDENTITY, message={"text": "NO"})

    assert result.status == RunStatus.COMPLETED
    assert result.values["outcome"] == "declined"
    assert "onboarding.declined" in harness.messenger.templates()


async def test_domain_blocked_terminal_for_corporate_email(make_harness):
    harness = make_harness(blocked_domains={"blocked.qa": "Other Co"})
    runtime = harness.platform.runtime
    await runtime.start("onboarding", EMAIL, EMAIL_ID, input={"trigger": "campaign"})
    result = await runtime.resume(EMAIL, EMAIL_ID, message={"text": "YES"})

    assert result.status == RunStatus.COMPLETED
    assert result.values["outcome"] == "domain_blocked"
    assert result.values["domain_block_reason"] == "blocked.qa"
    assert "onboarding.domain_blocked" in harness.messenger.templates()


async def test_ineligible_ends_flow_at_eligibility_update(make_harness):
    harness = make_harness(eligibility_result={"eligible": False})
    runtime = harness.platform.runtime

    async def resume(message):
        return await runtime.resume(WA, IDENTITY, message=message)

    await runtime.start("onboarding", WA, IDENTITY, input={"trigger": "campaign"})
    await resume({"text": "YES"})
    await resume({"first_name": "A", "last_name": "B"})
    await resume({"attachments": [{"filename": "CR.pdf"}]})
    result = await resume({"annual_revenue_qar": 1000})

    assert result.status == RunStatus.COMPLETED
    assert result.values["outcome"] == "not_eligible"
    assert result.values["eligible"] is False
    assert "onboarding.not_eligible" in harness.messenger.templates()


async def test_missing_documents_loops_until_complete(harness):
    runtime = harness.platform.runtime

    async def resume(message):
        return await runtime.resume(WA, IDENTITY, message=message)

    await runtime.start("onboarding", WA, IDENTITY, input={"trigger": "campaign"})
    await resume({"text": "YES"})
    await resume({"first_name": "A", "last_name": "B"})
    await resume({"attachments": [{"filename": "CR.pdf"}]})
    await resume({"annual_revenue_qar": 1000})
    await resume({"attachments": [{"filename": "Audited.pdf"}]})
    await resume({"name": "Buyer 1"})
    await resume({"shareholders": [{"name": "A", "percentage": 100}]})

    # Upload only one of the two required documents — graph re-asks.
    partial = await resume({"attachments": [{"filename": "Trade_License.pdf"}]})
    assert partial.status == RunStatus.WAITING_FOR_INPUT
    assert partial.prompt == {"waiting_for": "upload", "step": "documents"}
    assert partial.values["missing_documents"] == ["tax_card"]
    assert "onboarding.documents.missing" in harness.messenger.templates()

    # Upload the remaining doc → reaches the post-docs status poll.
    completed = await resume({"attachments": [{"filename": "Tax_Card.pdf"}]})
    assert completed.values["missing_documents"] == []
    assert "onboarding.documents.complete" in harness.messenger.templates()


async def test_not_qualified_post_payment_via_lender_status(make_harness):
    harness = make_harness()
    runtime = harness.platform.runtime

    async def resume(message):
        return await runtime.resume(WA, IDENTITY, message=message)

    await runtime.start("onboarding", WA, IDENTITY, input={"trigger": "campaign"})
    await resume({"text": "YES"})
    await resume({"first_name": "A", "last_name": "B"})
    await resume({"attachments": [{"filename": "CR.pdf"}]})
    await resume({"annual_revenue_qar": 1000})
    await resume({"attachments": [{"filename": "Audited.pdf"}]})
    await resume({"name": "Buyer 1"})
    await resume({"shareholders": [{"name": "A", "percentage": 100}]})
    await resume({"attachments": [{"filename": "Trade_License.pdf"}, {"filename": "Tax_Card.pdf"}]})

    # Advance to payment then back to lender wait.
    harness.identity.journey_status = "PRE_QUALIFIED"
    await resume({"type": "status_update"})
    await resume({"type": "payment", "paid": True})

    # Backend rejects at lender stage → NOT_ACCEPTED routes to not_qualified.
    harness.identity.journey_status = "NOT_ACCEPTED"
    result = await resume({"type": "status_update"})

    assert result.status == RunStatus.COMPLETED
    assert result.values["outcome"] == "not_qualified"
    assert "onboarding.not_qualified" in harness.messenger.templates()


async def test_terminal_success_via_activated_status(harness):
    """If the backend reaches ACTIVATED before we hit offers_fetch (race
    between webhook + poll), the activated terminal completes the flow."""

    runtime = harness.platform.runtime

    async def resume(message):
        return await runtime.resume(WA, IDENTITY, message=message)

    await runtime.start("onboarding", WA, IDENTITY, input={"trigger": "campaign"})
    await resume({"text": "YES"})
    await resume({"first_name": "A", "last_name": "B"})
    await resume({"attachments": [{"filename": "CR.pdf"}]})
    await resume({"annual_revenue_qar": 1000})
    await resume({"attachments": [{"filename": "Audited.pdf"}]})
    await resume({"name": "Buyer 1"})
    await resume({"shareholders": [{"name": "A", "percentage": 100}]})
    await resume({"attachments": [{"filename": "Trade_License.pdf"}, {"filename": "Tax_Card.pdf"}]})

    # Backend jumps straight to ACTIVATED at the post-docs poll.
    harness.identity.journey_status = "ACTIVATED"
    result = await resume({"type": "status_update"})

    assert result.status == RunStatus.COMPLETED
    assert result.values["outcome"] == "completed"
    assert "onboarding.activated" in harness.messenger.templates()
