"""Dispatcher: inbound starts then resumes; external resume via status_update."""

from __future__ import annotations

from types import SimpleNamespace

from app.shared.workflow import Channel, RunStatus

WA = Channel.WHATSAPP
IDENTITY = "+97455500003"
DOC = "ZHVtbXk="


async def test_inbound_starts_then_resumes_same_run(harness):
    dispatcher = harness.platform.dispatcher

    first = await dispatcher.inbound(WA, IDENTITY, text="hi")  # organic contact → start
    assert first.status == RunStatus.WAITING_FOR_INPUT
    assert first.prompt["step"] == "campaign"
    run_id = first.run.run_id

    # YES → resume same run. Post-main-merge the new-lead branch uses
    # create_user_if_missing=True so we land at consent_cr directly (no
    # collect_details step anymore).
    second = await dispatcher.inbound(WA, IDENTITY, text="YES")
    assert second.run.run_id == run_id
    assert second.prompt["step"] == "consent_cr"


async def test_on_inbound_with_message_object(harness):
    dispatcher = harness.platform.dispatcher
    message = SimpleNamespace(
        channel=WA, identity=IDENTITY, text="hi", attachments=[], message_id="m1"
    )
    result = await dispatcher.on_inbound(message)
    assert result.status == RunStatus.WAITING_FOR_INPUT
    assert result.prompt["step"] == "campaign"


async def test_resume_external_status_update(harness):
    """Drive the post-main spec-aligned flow:
    campaign → YES → CR upload → audited upload →
    PARK(prequalify_wait) → prequalification.completed webhook →
    documents upload → PARK(journey_wait) → status_update PRE_QUALIFIED → payment.
    """
    dispatcher = harness.platform.dispatcher
    runtime = harness.platform.runtime

    await runtime.start("onboarding", WA, IDENTITY, input={"trigger": "campaign"})
    await dispatcher.inbound(WA, IDENTITY, text="YES")  # → consent_cr
    await dispatcher.inbound(
        WA, IDENTITY, attachments=[{"filename": "CR.pdf", "content_base64": DOC}]
    )  # → financials
    await dispatcher.inbound(
        WA, IDENTITY, attachments=[{"filename": "Audited.pdf", "content_base64": DOC}]
    )  # → prequalify_wait

    # Release the prequalification gate (Postman/admin would emit this in
    # production; here we resume_external with the canonical event).
    await dispatcher.resume_external(
        WA, IDENTITY, {"event": "prequalification.completed", "madadScore": 78}
    )  # → documents

    # Bug #10a (2026-06-09): strict docs loop — one valid upload, admin
    # webhook to exit, then madad_score.ready triggers payment.
    await dispatcher.inbound(
        WA,
        IDENTITY,
        attachments=[
            {"filename": "Establishment_Card.pdf", "content_base64": DOC},
        ],
    )
    await dispatcher.resume_external(
        WA, IDENTITY, {"event": "documents.completed", "journey_status": "QUALIFIED"}
    )

    harness.identity.journey_status = "QUALIFIED"
    result = await dispatcher.resume_external(
        WA, IDENTITY, {"event": "madad_score.ready", "journey_status": "QUALIFIED"}
    )

    assert result.prompt["step"] == "payment"
