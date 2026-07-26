"""Cross-channel unify (2026-07-26): one SME, one state, two channels.

The onboarding run is KEYED by the SME's canonical (channel, identity) — for a
phone-having SME that's (WHATSAPP, phone), unchanged from before. When the SME
switches to email mid-flow, the inbound email must resume THAT SAME run (not
fork a second, drifting one) and the reply must go back out on EMAIL.

These tests pin the two invariants that protect the live SMEs:
  1. An email inbound from an SME with a live WhatsApp run resumes that run,
     replies on EMAIL, and creates NO second (EMAIL, …) session.
  2. A WhatsApp-only SME (the real one) is completely untouched: its inbound
     resumes its own WhatsApp run and never resolves cross-channel.
  3. A genuinely-new email lead (no registration match) falls through to the
     normal fresh-start path — no disruption, no accidental resume.
"""

from __future__ import annotations

import pytest

from app.shared.workflow import Channel

WA = Channel.WHATSAPP
EMAIL = Channel.EMAIL
PHONE = "+97455501101"
EMAIL_ADDR = "sme@tawfeeqtravel.qa"
DOC = "ZHVtbXk="


async def _drive_whatsapp_to_documents(harness) -> str:
    """Start a WhatsApp onboarding run and drive it to the documents-await
    state. Returns the run_id so tests can assert it is NOT forked."""

    runtime = harness.platform.runtime
    await runtime.start("onboarding", WA, PHONE, input={"trigger": "campaign"})
    await runtime.resume(WA, PHONE, message={"text": "YES"})
    await runtime.resume(WA, PHONE, message={"text": "biz@example.com"})
    await runtime.resume(
        WA, PHONE, message={"attachments": [{"filename": "CR.pdf", "content_base64": DOC}]}
    )
    await runtime.resume(
        WA, PHONE, message={"attachments": [{"filename": "Audited.pdf", "content_base64": DOC}]}
    )
    await runtime.resume(
        WA, PHONE, message={"event": "prequalification.completed", "journey_status": "PRE_QUALIFIED"},
    )
    session = await runtime.sessions.get(WA, PHONE)
    assert session is not None and session.active_run_id
    return session.active_run_id


async def test_email_switch_resumes_whatsapp_run_and_replies_on_email(harness) -> None:
    run_id = await _drive_whatsapp_to_documents(harness)

    # The backend now returns the phone on file for a registered email lookup;
    # model that on the in-memory identity fake.
    harness.identity.check_registration_overrides = {
        EMAIL_ADDR: {"registered": True, "phoneNumber": PHONE, "email": EMAIL_ADDR},
    }

    before = len(harness.messenger.sent)
    # SME switches to email and sends a document there.
    result = await harness.platform.dispatcher.inbound(
        EMAIL,
        EMAIL_ADDR,
        text="Here are my documents",
        attachments=[{"filename": "Establishment.pdf", "content_base64": DOC}],
    )
    assert result is not None

    # (1) The reply went back out on EMAIL, addressed to the email — not WhatsApp.
    new_sends = harness.messenger.sent[before:]
    email_sends = [s for s in new_sends if s["channel"] is EMAIL]
    assert email_sends, "the document reply must route on EMAIL"
    assert all(s["identity"] == EMAIL_ADDR for s in email_sends)
    # A WhatsApp switch-progress ping (Component C) is also expected — the only
    # allowed non-EMAIL send is that ping, to the canonical WhatsApp identity.
    for s in new_sends:
        if s["channel"] is not EMAIL:
            assert s["channel"] is WA and s["identity"] == PHONE, (
                f"unexpected non-email send: {s['channel']}/{s['identity']}"
            )

    # (2) The SAME canonical WhatsApp run advanced — NOT a forked new run.
    wa_session = await harness.platform.runtime.sessions.get(WA, PHONE)
    assert wa_session is not None and wa_session.active_run_id == run_id

    # (3) No second session was ever created for the email identity — the whole
    #     point of unify is ONE run, keyed canonically on WhatsApp.
    email_session = await harness.platform.runtime.sessions.get(EMAIL, EMAIL_ADDR)
    assert email_session is None, "cross-channel resume must NOT fork an EMAIL session"


async def test_whatsapp_only_sme_is_untouched(harness) -> None:
    """The real SME path: a WhatsApp inbound with a live WhatsApp run resumes
    its own run and replies on WhatsApp — no cross-channel resolution, no email."""

    run_id = await _drive_whatsapp_to_documents(harness)
    before = len(harness.messenger.sent)

    result = await harness.platform.dispatcher.inbound(
        WA, PHONE, text="Any update on my application?",
    )
    assert result is not None

    new_sends = harness.messenger.sent[before:]
    assert new_sends
    assert all(s["channel"] is WA for s in new_sends)
    assert all(s["identity"] == PHONE for s in new_sends)
    # Same run, no email fork.
    wa_session = await harness.platform.runtime.sessions.get(WA, PHONE)
    assert wa_session is not None and wa_session.active_run_id == run_id
    assert await harness.platform.runtime.sessions.get(EMAIL, EMAIL_ADDR) is None


async def test_unregistered_email_lead_falls_through_to_fresh_start(harness) -> None:
    """A genuinely new email address (no registration match) must NOT resolve to
    anyone's run — it starts its own, exactly as before this feature."""

    # No check_registration_overrides ⇒ the fake returns {registered: False}.
    result = await harness.platform.dispatcher.inbound(
        EMAIL, "brand-new@somebiz.qa", text="Hi, I want financing",
    )
    assert result is not None
    # A fresh EMAIL-keyed session exists for this new lead (normal path).
    session = await harness.platform.runtime.sessions.get(EMAIL, "brand-new@somebiz.qa")
    assert session is not None and session.active_run_id
