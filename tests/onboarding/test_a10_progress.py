"""A10 — update_onboarding_progress fires at the canonical 1–8 milestones.

Per Ishan (2026-06-07): WhatsApp lead conversational steps drive backend
gating. Most critical: step=3 MUST be set after the audited financial report
upload + account-created message — otherwise the pre-qualified document
checklist won't fire from the backend.
"""

from __future__ import annotations

from app.shared.workflow import Channel

WA = Channel.WHATSAPP
EMAIL = Channel.EMAIL


async def _drive_full_path(harness, identity: str):
    runtime = harness.platform.runtime
    doc = "ZHVtbXk="

    async def resume(message):
        return await runtime.resume(WA, identity, message=message)

    await runtime.start("onboarding", WA, identity, input={"trigger": "campaign"})
    await resume({"text": "YES"})
    await resume({"attachments": [{"filename": "CR.pdf", "content_base64": doc}]})
    await resume({"attachments": [{"filename": "Audited.pdf", "content_base64": doc}]})
    await resume({"event": "prequalification.completed", "madadScore": 78})
    # Bug #10a (2026-06-09): docs loop is strict — one valid upload + admin
    # webhook exit, then madad_score.ready triggers payment.
    await resume(
        {"attachments": [{"filename": "Establishment_Card.pdf", "content_base64": doc}]}
    )
    await resume({"event": "documents.completed", "journey_status": "QUALIFIED"})
    harness.identity.journey_status = "QUALIFIED"
    await resume({"event": "madad_score.ready", "journey_status": "QUALIFIED"})
    await resume({"type": "payment", "paid": True})
    harness.identity.journey_status = "ACCEPTED"
    return await resume({"type": "status_update"})


async def test_progress_steps_1_through_8_fire_in_order(harness) -> None:
    """The 8 canonical milestones each fire update_onboarding_progress with
    the right step number, monotonically increasing."""
    await _drive_full_path(harness, "+97455500A10")

    progress_calls = [
        kwargs.get("step")
        for name, kwargs in harness.identity.calls
        if name == "update_onboarding_progress"
    ]
    # All 8 steps fired exactly once, in order.
    assert progress_calls == [1, 2, 3, 4, 5, 6, 7, 8]


async def test_step_3_is_the_backend_hard_gate(harness) -> None:
    """The PDF Step 3 gate (audited financials upload + account.created msg)
    MUST trigger update_onboarding_progress(step=3) per Ishan's backend
    contract — backend won't fire the pre-qualified checklist otherwise."""
    runtime = harness.platform.runtime
    identity = "+97455500A11"
    doc = "ZHVtbXk="

    async def resume(message):
        return await runtime.resume(WA, identity, message=message)

    await runtime.start("onboarding", WA, identity, input={"trigger": "campaign"})
    await resume({"text": "YES"})
    await resume({"attachments": [{"filename": "CR.pdf", "content_base64": doc}]})
    # BEFORE audited: progress has been called for step 1 (lead created) and
    # step 2 (CR uploaded) — NOT yet step 3.
    progress_so_far = [
        kwargs.get("step")
        for name, kwargs in harness.identity.calls
        if name == "update_onboarding_progress"
    ]
    assert 3 not in progress_so_far

    # Audited upload triggers step=3.
    await resume({"attachments": [{"filename": "Audited.pdf", "content_base64": doc}]})
    progress_after = [
        kwargs.get("step")
        for name, kwargs in harness.identity.calls
        if name == "update_onboarding_progress"
    ]
    assert 3 in progress_after


async def test_email_path_does_not_call_progress(make_harness) -> None:
    """update_onboarding_progress is a WhatsApp-only concern per Ishan's
    contract. Email leads must NOT trigger it."""
    harness = make_harness()
    runtime = harness.platform.runtime
    identity = "newhire@example.com"
    doc = "ZHVtbXk="

    async def resume(message):
        return await runtime.resume(EMAIL, identity, message=message)

    await runtime.start("onboarding", EMAIL, identity, input={"trigger": "campaign"})
    await resume({"text": "YES"})
    await resume({"attachments": [{"filename": "CR.pdf", "content_base64": doc}]})
    await resume({"attachments": [{"filename": "Audited.pdf", "content_base64": doc}]})

    call_names = [name for name, _ in harness.identity.calls]
    assert "update_onboarding_progress" not in call_names


async def test_progress_does_not_regress_on_retry(harness) -> None:
    """If the same step fires twice (e.g. duplicate inbound or retry), the
    helper should be idempotent — don't re-call with the same or older step."""
    # Drive past step 3.
    await _drive_full_path(harness, "+97455500A12")

    # Pull just the step=3 calls (should be exactly 1).
    step_3_calls = [
        1
        for name, kwargs in harness.identity.calls
        if name == "update_onboarding_progress" and kwargs.get("step") == 3
    ]
    assert sum(step_3_calls) == 1
