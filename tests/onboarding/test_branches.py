"""Onboarding decision branches: decline, not-eligible, missing-docs loop, not-qualified."""

from __future__ import annotations

from app.shared.workflow import Channel, RunStatus

WA = Channel.WHATSAPP
IDENTITY = "+97455500002"


async def test_decline_at_campaign_entry(harness):
    runtime = harness.platform.runtime
    await runtime.start("onboarding", WA, IDENTITY, input={"trigger": "campaign"})
    result = await runtime.resume(WA, IDENTITY, message={"text": "NO"})

    assert result.status == RunStatus.COMPLETED
    assert result.values["outcome"] == "declined"
    assert "onboarding.declined" in harness.messenger.templates()


async def test_not_eligible_ends_flow(make_harness):
    harness = make_harness(eligible=False)
    runtime = harness.platform.runtime
    await runtime.start("onboarding", WA, IDENTITY, input={"trigger": "campaign"})
    await runtime.resume(WA, IDENTITY, message={"text": "YES"})
    result = await runtime.resume(WA, IDENTITY, message={"attachments": [{"filename": "CR.pdf"}]})

    assert result.status == RunStatus.COMPLETED
    assert result.values["outcome"] == "not_eligible"
    assert result.values["eligible"] is False
    assert "onboarding.not_eligible" in harness.messenger.templates()


async def test_not_prequalified_ends_flow(harness):
    runtime = harness.platform.runtime
    await runtime.start("onboarding", WA, IDENTITY, input={"trigger": "campaign"})
    await runtime.resume(WA, IDENTITY, message={"text": "YES"})
    await runtime.resume(WA, IDENTITY, message={"attachments": [{"filename": "CR.pdf"}]})
    await runtime.resume(WA, IDENTITY, message={"attachments": [{"filename": "Audited.pdf"}]})
    result = await runtime.resume(
        WA, IDENTITY, message={"type": "prequalification", "qualified": False}
    )

    assert result.values["outcome"] == "not_prequalified"
    assert "onboarding.not_prequalified" in harness.messenger.templates()


async def test_missing_documents_loop(harness):
    runtime = harness.platform.runtime
    await runtime.start("onboarding", WA, IDENTITY, input={"trigger": "campaign"})
    await runtime.resume(WA, IDENTITY, message={"text": "YES"})
    await runtime.resume(WA, IDENTITY, message={"attachments": [{"filename": "CR.pdf"}]})
    await runtime.resume(WA, IDENTITY, message={"attachments": [{"filename": "Audited.pdf"}]})
    await runtime.resume(WA, IDENTITY, message={"type": "prequalification", "qualified": True})

    # Upload only one of the two required documents -> still waiting, missing list sent.
    partial = await runtime.resume(
        WA, IDENTITY, message={"attachments": [{"filename": "Trade_License.pdf"}]}
    )
    assert partial.status == RunStatus.WAITING_FOR_INPUT
    assert partial.values["missing_documents"] == ["tax_card"]
    assert "onboarding.documents.missing" in harness.messenger.templates()

    # Upload the rest -> proceeds to risk assessment.
    completed_docs = await runtime.resume(
        WA, IDENTITY, message={"attachments": [{"filename": "Tax_Card.pdf"}]}
    )
    assert completed_docs.prompt == {"waiting_for": "score", "step": "risk_assessment"}
    assert "onboarding.documents.complete" in harness.messenger.templates()


async def test_not_qualified_by_risk(harness):
    runtime = harness.platform.runtime
    await runtime.start("onboarding", WA, IDENTITY, input={"trigger": "campaign"})
    await runtime.resume(WA, IDENTITY, message={"text": "YES"})
    await runtime.resume(WA, IDENTITY, message={"attachments": [{"filename": "CR.pdf"}]})
    await runtime.resume(WA, IDENTITY, message={"attachments": [{"filename": "Audited.pdf"}]})
    await runtime.resume(WA, IDENTITY, message={"type": "prequalification", "qualified": True})
    await runtime.resume(
        WA,
        IDENTITY,
        message={"attachments": [{"filename": "Trade_License.pdf"}, {"filename": "Tax_Card.pdf"}]},
    )
    result = await runtime.resume(
        WA, IDENTITY, message={"type": "score", "score": 30, "qualified": False}
    )

    assert result.values["outcome"] == "not_qualified"
    assert "onboarding.not_qualified" in harness.messenger.templates()
