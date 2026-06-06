"""Dispatcher: inbound starts then resumes; external resume via status_update."""

from __future__ import annotations

from types import SimpleNamespace

from app.shared.workflow import Channel, RunStatus

WA = Channel.WHATSAPP
IDENTITY = "+97455500003"


async def test_inbound_starts_then_resumes_same_run(harness):
    dispatcher = harness.platform.dispatcher

    first = await dispatcher.inbound(WA, IDENTITY, text="hi")  # organic contact → start
    assert first.status == RunStatus.WAITING_FOR_INPUT
    assert first.prompt["step"] == "campaign"
    run_id = first.run.run_id

    second = await dispatcher.inbound(WA, IDENTITY, text="YES")  # YES → resume same run
    assert second.run.run_id == run_id
    assert second.prompt["step"] == "collect_details"


async def test_on_inbound_with_message_object(harness):
    dispatcher = harness.platform.dispatcher
    message = SimpleNamespace(
        channel=WA, identity=IDENTITY, text="hi", attachments=[], message_id="m1"
    )
    result = await dispatcher.on_inbound(message)
    assert result.status == RunStatus.WAITING_FOR_INPUT
    assert result.prompt["step"] == "campaign"


async def test_resume_external_status_update(harness):
    dispatcher = harness.platform.dispatcher
    runtime = harness.platform.runtime

    await runtime.start("onboarding", WA, IDENTITY, input={"trigger": "campaign"})
    await dispatcher.inbound(WA, IDENTITY, text="YES")
    await dispatcher.inbound(WA, IDENTITY, text="Aisha Karim")
    await dispatcher.inbound(WA, IDENTITY, attachments=[{"filename": "CR.pdf"}])
    # Eligibility form payload (no attachments / text — pure dict).
    await dispatcher.resume_external(
        WA, IDENTITY, {"annual_revenue_qar": 1000}
    )
    await dispatcher.inbound(WA, IDENTITY, attachments=[{"filename": "Audited.pdf"}])
    await dispatcher.resume_external(WA, IDENTITY, {"name": "ACME"})
    await dispatcher.resume_external(
        WA, IDENTITY, {"shareholders": [{"name": "A", "phoneNumber": "+97455500001"}]}
    )
    await dispatcher.inbound(
        WA,
        IDENTITY,
        attachments=[{"filename": "Trade_License.pdf"}, {"filename": "Tax_Card.pdf"}],
    )

    # Webhook-driven status update advances the journey out of journey_wait.
    harness.identity.journey_status = "PRE_QUALIFIED"
    result = await dispatcher.resume_external(WA, IDENTITY, {"type": "status_update"})

    assert result.prompt["step"] == "payment"
