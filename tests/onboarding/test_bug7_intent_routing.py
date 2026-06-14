"""Bug #7+#8 (2026-06-09): pre-qual + payment wait nodes returned the same
generic 'still pending' reply to every off-script chat — including
direct status questions — because the OpenAI key was 401-ing and every
call collapsed to the canned fallback.

The wait nodes already had typed answer helpers (_safe_status_answer /
_safe_portal_answer / _off_script_template); they weren't being
consulted because _smart_contextual jumped straight to the LLM path.
The new _contextual_off_script helper checks intent first so a status
question gets the status answer even with the LLM offline.
"""

from __future__ import annotations

from app.shared.workflow import Channel

WA = Channel.WHATSAPP
IDENTITY = "+97455500701"
DOC = "ZHVtbXk="


async def _drive_to_prequalify_wait(harness) -> None:
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


async def test_status_question_at_prequal_wait_gets_status_answer(harness) -> None:
    """A 'what's my status' question while parked at prequal_wait must
    NOT collapse to the generic 'pre-qualification will be ready soon'
    fallback — it should produce a status-flavored answer even when the
    LLM is offline."""
    await _drive_to_prequalify_wait(harness)

    result = await harness.platform.runtime.resume(
        WA, IDENTITY, message={"text": "what's my application status?"}
    )

    assert result.prompt == {
        "waiting_for": "prequalification",
        "step": "prequalify_wait",
    }
    # The contextual template fires (any intent leads to it), but the
    # answer must come from _safe_status_answer, not the canned fallback.
    sent = [
        s for s in harness.messenger.sent
        if s["template_key"] == "onboarding.help.contextual"
    ]
    assert sent
    last = sent[-1]["variables"]["answer"]
    # The canned prequal fallback contains 'pre-qualification result will be ready'
    assert "pre-qualification result will be ready" not in last


async def test_portal_question_at_prequal_wait_gets_portal_answer(harness) -> None:
    await _drive_to_prequalify_wait(harness)

    await harness.platform.runtime.resume(
        WA, IDENTITY, message={"text": "how do I login to madadfintech?"}
    )

    sent = [
        s for s in harness.messenger.sent
        if s["template_key"] == "onboarding.help.contextual"
    ]
    assert sent
    last = sent[-1]["variables"]["answer"]
    # Should NOT be the generic prequal fallback.
    assert "pre-qualification result will be ready" not in last
