"""User UAT 2026-06-10: classifier failures (notably AoA) leave required
slots permanently "still needed" even when the SME has uploaded enough
files. Plus the SME often realises they have one more document to send
right after the coffee message — currently the flow forces them past it.

Two changes pinned here:

1. Count-based unblock at the workflow level. ``_route_documents`` now
   exits to ``complete`` when the cumulative attachment count meets the
   required count, regardless of how many slots are still "pending" on
   ``state.missing_documents``. Mirrors the doc-service-level unblock
   in PR #4 (commit 6c05b1c).

2. New ``more_docs_prompt`` step after ``documents_complete``. The SME
   is asked YES / NO whether they want to send more docs. YES loops
   back to the upload-await node; NO proceeds to ``payment_wait_await``.
"""

from __future__ import annotations

from app.services.workflow.onboarding import (
    DEFAULT_WHATSAPP_REQUIRED_DOCS,
    OnboardingWorkflow,
)
from app.services.workflow.state import OnboardingState
from app.shared.workflow import Channel

WA = Channel.WHATSAPP
IDENTITY = "+97455501801"
DOC = "ZHVtbXk="


def _wf() -> OnboardingWorkflow:
    # White-box access for the pure router functions.
    from app.services.workflow.deps import build_onboarding_platform

    return build_onboarding_platform().workflow


def test_route_documents_unblocks_on_count_with_missing_slots() -> None:
    """Classifier hung on a few docs → required slots stay pending →
    the count-based unblock kicks in and lets the loop complete."""
    wf = _wf()
    state = OnboardingState(
        identity=IDENTITY,
        missing_documents=["aoa", "proof_of_address"],  # still pending
        docs_uploaded_count=len(DEFAULT_WHATSAPP_REQUIRED_DOCS),  # but enough sent
    )
    assert wf._route_documents(state) == "complete"  # noqa: SLF001


def test_route_documents_does_not_unblock_below_threshold() -> None:
    """1 upload vs 10 required must NOT unblock — guard against a
    too-eager loop exit."""
    wf = _wf()
    state = OnboardingState(
        identity=IDENTITY,
        missing_documents=list(DEFAULT_WHATSAPP_REQUIRED_DOCS),
        docs_uploaded_count=1,
    )
    assert wf._route_documents(state) == "await_again"  # noqa: SLF001


def test_route_documents_natural_completion_still_works() -> None:
    """When every required slot is filled (missing list empty), the
    route still says ``complete`` — count-based path is additive."""
    wf = _wf()
    state = OnboardingState(identity=IDENTITY, missing_documents=[])
    assert wf._route_documents(state) == "complete"  # noqa: SLF001


def test_route_more_docs_dispatches_on_decision() -> None:
    wf = _wf()
    yes_state = OnboardingState(identity=IDENTITY, more_docs_decision="yes")
    no_state = OnboardingState(identity=IDENTITY, more_docs_decision="no")
    pending_state = OnboardingState(identity=IDENTITY, more_docs_decision=None)
    assert wf._route_more_docs(yes_state) == "yes"  # noqa: SLF001
    assert wf._route_more_docs(no_state) == "no"  # noqa: SLF001
    assert wf._route_more_docs(pending_state) == "await_again"  # noqa: SLF001


async def _drive_to_more_docs_prompt(harness) -> None:
    """End-to-end: campaign → YES → CR → audited → prequal → enough
    uploads to trigger the count-based unblock → coffee + more-docs
    prompt fires."""

    runtime = harness.platform.runtime
    await runtime.start("onboarding", WA, IDENTITY, input={"trigger": "campaign"})
    await runtime.resume(WA, IDENTITY, message={"text": "YES"})
    await runtime.resume(WA, IDENTITY, message={"text": "biz@example.com"})  # business_email
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
    # Send enough attachments to hit the count-based unblock — none are
    # required-named in the in-memory classifier so missing_documents stays
    # populated; the count-based exit is what we're exercising.
    n = len(DEFAULT_WHATSAPP_REQUIRED_DOCS)
    for idx in range(n):
        await runtime.resume(
            WA, IDENTITY,
            message={
                "attachments": [
                    {"filename": f"random_{idx}.pdf", "content_base64": DOC}
                ]
            },
        )


async def test_count_unblock_drives_into_more_docs_prompt(harness) -> None:
    """After N uploads (N == required count), the run parks at the new
    ``more_docs_prompt_await`` step instead of crossing straight into
    payment_wait."""
    await _drive_to_more_docs_prompt(harness)

    runtime = harness.platform.runtime
    session = await runtime.sessions.get(WA, IDENTITY)
    assert session is not None and session.active_run_id
    run = await runtime.run_store.get(session.active_run_id)
    assert run.current_step == "more_docs_prompt_await"
    # And the new prompt template fired.
    assert (
        "onboarding.documents.more_docs_prompt"
        in harness.messenger.templates()
    )
    # Plus the coffee message — natural-completion narrative is preserved.
    assert "onboarding.documents.complete" in harness.messenger.templates()


async def test_more_docs_yes_loops_back_to_upload_await(harness) -> None:
    """SME replies YES → run resumes the upload-await node, ready to
    accept another batch."""
    await _drive_to_more_docs_prompt(harness)

    runtime = harness.platform.runtime
    result = await runtime.resume(WA, IDENTITY, message={"text": "YES"})
    await runtime.resume(WA, IDENTITY, message={"text": "biz@example.com"})  # business_email

    assert result.prompt == {"waiting_for": "upload", "step": "documents"}


async def test_more_docs_no_advances_to_payment_wait(harness) -> None:
    """SME replies NO → run advances to payment_wait_await."""
    await _drive_to_more_docs_prompt(harness)

    runtime = harness.platform.runtime
    result = await runtime.resume(WA, IDENTITY, message={"text": "NO"})

    assert result.prompt == {"waiting_for": "payment_ready", "step": "payment_wait"}


async def test_more_docs_off_script_re_prompts(harness) -> None:
    """An unrelated chat reply (not YES/NO) gets an answer and stays
    parked at the prompt — never silently advances."""
    await _drive_to_more_docs_prompt(harness)

    runtime = harness.platform.runtime
    result = await runtime.resume(
        WA, IDENTITY, message={"text": "is my application ok?"}
    )

    assert result.prompt == {"waiting_for": "reply", "step": "more_docs_prompt"}
