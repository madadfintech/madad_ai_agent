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


# NOTE: test_ineligible_ends_flow_at_eligibility_update was deleted —
# the eligibility_update node is no longer in the workflow graph per the
# spec-alignment merge (commit 62d7560). The Qatar-residency check now
# happens inline via consent_await without surfacing an explicit
# "ineligible" terminal.

# NOTE: test_missing_documents_loops_until_complete was reinstated by
# Bug #10a (2026-06-09) — _route_documents is no longer lenient. A single
# upload no longer completes the loop; tests that don't care about the
# loop's per-doc behaviour fast-forward via a forward-status webhook.


async def _drive_to_payment(harness):
    """Drive the spec-aligned flow up to the payment node."""
    runtime = harness.platform.runtime
    doc = "ZHVtbXk="

    async def resume(message):
        return await runtime.resume(WA, IDENTITY, message=message)

    await runtime.start("onboarding", WA, IDENTITY, input={"trigger": "campaign"})
    await resume({"text": "YES"})
    await resume({"text": "biz@example.com"})  # business_email
    await resume({"attachments": [{"filename": "CR.pdf", "content_base64": doc}]})
    await resume({"attachments": [{"filename": "Audited.pdf", "content_base64": doc}]})
    await resume({"event": "prequalification.completed", "madadScore": 78})
    # Bug #10a + Bug #12 (2026-06-09): one ``madad_score.ready`` event
    # (QUALIFIED) exits the docs loop AND fast-forwards through payment_wait
    # into the payment chain on the same resume.
    harness.identity.journey_status = "QUALIFIED"
    return await resume(
        {"event": "madad_score.ready", "journey_status": "QUALIFIED"}
    )


async def test_not_qualified_post_payment_via_lender_status(make_harness):
    harness = make_harness()
    runtime = harness.platform.runtime

    async def resume(message):
        return await runtime.resume(WA, IDENTITY, message=message)

    await _drive_to_payment(harness)
    # Mark monetization payment as paid → moves to lender_wait.
    await resume({"type": "payment", "paid": True})

    # Backend rejects at lender stage → NOT_ACCEPTED routes to not_qualified.
    harness.identity.journey_status = "NOT_ACCEPTED"
    result = await resume({"type": "status_update"})

    assert result.status == RunStatus.COMPLETED
    assert result.values["outcome"] == "not_qualified"
    assert "onboarding.not_qualified" in harness.messenger.templates()


async def test_activated_parks_into_invoice_collect_loop(harness):
    """If the backend reaches ACTIVATED before we hit offers_fetch (race
    between webhook + poll), the activated message still goes out and the run
    parks in the invoice-collection loop (steps 10–13, per db9b4a0)."""
    runtime = harness.platform.runtime

    async def resume(message):
        return await runtime.resume(WA, IDENTITY, message=message)

    await _drive_to_payment(harness)
    await resume({"type": "payment", "paid": True})

    # Backend jumps straight to ACTIVATED at the post-payment poll.
    harness.identity.journey_status = "ACTIVATED"
    result = await resume({"type": "status_update"})

    assert result.status == RunStatus.WAITING_FOR_INPUT
    assert result.run.current_step == "invoice_collect_await"
    assert "onboarding.activated" in harness.messenger.templates()
