"""Bug #1 (2026-06-09): CR upload silence.

QA reported the SME sees no reply at all when the CR document is uploaded —
not even the financials prompt. Ishan's handover §9 traced it to the
``consent_await → cr_upload_base64 → financials_send`` chain where the only
user-facing reply lived at the very end. A transient messenger / token /
progress failure dropped the run with no user notification.

The fix: send ``onboarding.cr.received`` immediately the instant a valid CR
attachment is detected in ``consent_await``, BEFORE any MCP write — so the
SME always sees a confirmation regardless of downstream hiccups. The
financials prompt then follows. ``_financials_send`` is also guarded so a
messenger failure there cannot kill the run.
"""

from __future__ import annotations

from app.shared.workflow import Channel

WA = Channel.WHATSAPP
IDENTITY = "+97455500001"
CR_BYTES = "ZHVtbXk="  # base64 of "dummy"


async def test_valid_cr_triggers_immediate_ack(make_harness) -> None:
    harness = make_harness(known_phones={IDENTITY: "user_42"})
    runtime = harness.platform.runtime
    await runtime.start("onboarding", WA, IDENTITY, input={"trigger": "campaign"})
    await runtime.resume(WA, IDENTITY, message={"text": "YES"})
    await runtime.resume(WA, IDENTITY, message={"text": "biz@example.com"})  # business_email

    await runtime.resume(
        WA,
        IDENTITY,
        message={
            "attachments": [
                {
                    "filename": "CR.pdf",
                    "content_base64": CR_BYTES,
                    "mime_type": "application/pdf",
                }
            ]
        },
    )

    templates = harness.messenger.templates()
    # The ack must fire BEFORE the financials prompt so the user is never
    # silent-failed if the financials send hiccups.
    assert "onboarding.cr.received" in templates
    cr_ix = templates.index("onboarding.cr.received")
    fin_ix = templates.index("onboarding.financials.request")
    assert cr_ix < fin_ix
