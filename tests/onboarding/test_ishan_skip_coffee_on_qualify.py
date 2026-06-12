"""Ishan refinement (UAT 2026-06-09): when admin QUALIFIES mid-docs-loop,
skip the misleading "🎊 all documents received" coffee message and route
straight to the payment chain.

Two distinct end-states the docs-loop now produces:

  * Natural completion — every required doc landed. The coffee message
    is honest ("all docs received"); send it + park at payment_wait
    until backend fires madad_score.ready.
  * Admin override — admin QUALIFIES with the checklist still missing
    items. Skip the coffee message (it would lie about the SME's
    state) and route directly to the payment chain. Fire the step=5
    progress marker on this path too so backend sees the canonical
    ordered sequence regardless of route.
"""

from __future__ import annotations

from app.shared.workflow import Channel

WA = Channel.WHATSAPP
IDENTITY = "+97455501301"
DOC = "ZHVtbXk="


async def _drive_to_documents(harness) -> None:
    runtime = harness.platform.runtime
    await runtime.start("onboarding", WA, IDENTITY, input={"trigger": "campaign"})
    await runtime.resume(WA, IDENTITY, message={"text": "YES"})
    await runtime.resume(
        WA, IDENTITY, message={"attachments": [{"filename": "CR.pdf", "content_base64": DOC}]}
    )
    await runtime.resume(
        WA,
        IDENTITY,
        message={"attachments": [{"filename": "Audited.pdf", "content_base64": DOC}]},
    )
    await runtime.resume(
        WA,
        IDENTITY,
        message={"event": "prequalification.completed", "journey_status": "PRE_QUALIFIED"},
    )


async def test_qualify_override_skips_coffee_message(harness) -> None:
    """Admin QUALIFIES mid-docs-loop while the checklist is still
    missing items. The coffee message must NOT fire (it would
    misrepresent state); the run jumps straight to the payment chain."""
    await _drive_to_documents(harness)

    result = await harness.platform.runtime.resume(
        WA,
        IDENTITY,
        message={
            "event": "madad_score.ready",
            "journey_status": "QUALIFIED",
            "madadScore": 81,
        },
    )

    templates = harness.messenger.templates()
    assert "onboarding.documents.complete" not in templates, (
        "coffee message must NOT fire when admin overrides the checklist"
    )
    assert "onboarding.documents.checklist" in templates  # the pre-qual ask
    assert (
        "onboarding.payment.request.button" in templates
        or "onboarding.payment.request" in templates
    )
    assert result.prompt == {"waiting_for": "payment", "step": "payment"}


async def test_qualify_override_fires_step_5_progress_marker(harness) -> None:
    """Backend tracks the canonical 1-8 conversational steps via
    ``update_onboarding_progress``. Step 5 used to fire only inside
    ``_documents_complete`` (the coffee node); now that node is
    bypassed on admin override, fire step 5 from the fast-forward
    branch too so the sequence stays whole."""
    await _drive_to_documents(harness)
    await harness.platform.runtime.resume(
        WA,
        IDENTITY,
        message={
            "event": "madad_score.ready",
            "journey_status": "QUALIFIED",
            "madadScore": 81,
        },
    )

    progress_calls = [
        kwargs.get("step")
        for name, kwargs in harness.identity.calls
        if name == "update_onboarding_progress"
    ]
    # Every step up to 6 must have fired, in order. The earlier ones
    # come from prior nodes; step 5 is the one this fix guarantees.
    assert 5 in progress_calls, (
        f"step=5 progress marker should fire on admin-override path; got {progress_calls}"
    )
