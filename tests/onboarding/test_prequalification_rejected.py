"""UAT 2026-06-17 gap fix: prequalification.rejected path.

Before this fix, when the admin marked the SME as not pre-qualified the
SME parked at ``prequalify_wait_await`` forever — no event handler, no
template, no terminal. Spec PDF Step 3 (negative branch) calls for a
clear next-steps message:

  "After reviewing your business profile, we are unable to pre-qualify
   your business for financing at this time."

These tests pin the end-to-end contract: webhook arrives → translation →
state flag → terminal node → SME-facing template → run completes.
"""

from __future__ import annotations

from app.services.workflow.dispatcher import translate_backend_event
from app.shared.workflow import Channel, RunStatus

WA = Channel.WHATSAPP
IDENTITY = "+97455500091"


async def _drive_to_prequalify_wait(harness) -> None:
    """Drive a fresh run up to the prequalify_wait_await node."""
    runtime = harness.platform.runtime
    doc = "ZHVtbXk="

    async def resume(message):
        return await runtime.resume(WA, IDENTITY, message=message)

    await runtime.start("onboarding", WA, IDENTITY, input={"trigger": "campaign"})
    await resume({"text": "YES"})
    await resume({"text": "biz@example.com"})  # business_email
    await resume({"attachments": [{"filename": "CR.pdf", "content_base64": doc}]})
    await resume({"attachments": [{"filename": "Audited.pdf", "content_base64": doc}]})


async def test_prequalification_rejected_terminates_with_template(harness) -> None:
    """Webhook arrives → translation → state flag → terminal + template."""
    await _drive_to_prequalify_wait(harness)
    runtime = harness.platform.runtime

    payload = translate_backend_event("prequalification.rejected", {})
    result = await runtime.resume(WA, IDENTITY, message=payload)

    assert result.status == RunStatus.COMPLETED
    assert result.values["outcome"] == "not_pre_qualified"
    assert "onboarding.not_pre_qualified" in harness.messenger.templates()


def test_translate_backend_event_marks_rejection_flag() -> None:
    """Dispatcher tags the resume payload so ``_prequalify_wait_await``
    routes to the rejection terminal."""
    payload = translate_backend_event("prequalification.rejected", {})

    assert payload["type"] == "status_update"
    assert payload["event"] == "prequalification.rejected"
    assert payload["prequalification_rejected"] is True


def test_prequalification_rejected_in_phase1a_event_set() -> None:
    """Webhook receiver accepts the new event without an allow-list update."""
    from app.services.workflow.dispatcher import PHASE1A_BACKEND_EVENTS

    assert "prequalification.rejected" in PHASE1A_BACKEND_EVENTS
