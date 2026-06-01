"""Dispatcher: inbound starts then resumes; external webhook resume."""

from __future__ import annotations

from types import SimpleNamespace

from app.shared.workflow import Channel, RunStatus

WA = Channel.WHATSAPP
IDENTITY = "+97455500003"


async def test_inbound_starts_then_resumes_same_run(harness):
    dispatcher = harness.platform.dispatcher

    first = await dispatcher.inbound(WA, IDENTITY, text="hi")  # organic contact -> start
    assert first.status == RunStatus.WAITING_FOR_INPUT
    assert first.prompt["step"] == "campaign"
    run_id = first.run.run_id

    second = await dispatcher.inbound(WA, IDENTITY, text="YES")  # reply -> resume same run
    assert second.run.run_id == run_id
    assert second.prompt["step"] == "consent_cr"


async def test_on_inbound_with_message_object(harness):
    dispatcher = harness.platform.dispatcher
    # Duck-typed communication Message (channel, identity, text, attachments).
    message = SimpleNamespace(
        channel=WA, identity=IDENTITY, text="hi", attachments=[], message_id="m1"
    )
    result = await dispatcher.on_inbound(message)
    assert result.status == RunStatus.WAITING_FOR_INPUT
    assert result.prompt["step"] == "campaign"


async def test_resume_external_webhook(harness):
    dispatcher = harness.platform.dispatcher
    runtime = harness.platform.runtime

    await runtime.start("onboarding", WA, IDENTITY, input={"trigger": "campaign"})
    await dispatcher.inbound(WA, IDENTITY, text="YES")
    await dispatcher.inbound(WA, IDENTITY, attachments=[{"filename": "CR.pdf"}])
    await dispatcher.inbound(WA, IDENTITY, attachments=[{"filename": "Audited.pdf"}])

    # External pre-qualification decision arrives via webhook.
    result = await dispatcher.resume_external(
        WA, IDENTITY, {"type": "prequalification", "qualified": True}
    )
    assert result.prompt["step"] == "documents"
    assert result.values["prequalified"] is True
