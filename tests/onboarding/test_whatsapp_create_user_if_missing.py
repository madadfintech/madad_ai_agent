"""A12 — WhatsApp organic-entry uses create_user_if_missing=True.

Per Ishan (2026-06-07): WhatsApp new-leads skip the collect_details +
complete_onboarding hops. A single ``open_session(create_user_if_missing=True)``
call mints a SIGN_UP account from the phone alone and returns the access_token
directly. Email new-leads still need the full path.
"""

from __future__ import annotations

from app.shared.workflow import Channel

WA = Channel.WHATSAPP
EMAIL = Channel.EMAIL
PHONE = "+97455500A12"
EMAIL_ID = "newhire@example.com"


async def test_whatsapp_new_lead_uses_create_user_if_missing(harness) -> None:
    """check_contact returns new + channel is WhatsApp → router picks
    new_whatsapp → channel_session_create_user fires with
    create_user_if_missing=True, NOT complete_onboarding."""
    runtime = harness.platform.runtime
    await runtime.start("onboarding", WA, PHONE, input={"trigger": "campaign"})
    result = await runtime.resume(WA, PHONE, message={"text": "YES"})

    identity_calls = [name for name, _ in harness.identity.calls]
    # The new-lead WhatsApp path bypasses complete_onboarding entirely.
    assert "complete_onboarding" not in identity_calls
    # open_session was called with create_user_if_missing=True.
    open_session_payloads = [
        kwargs for name, kwargs in harness.identity.calls if name == "open_session"
    ]
    assert any(p.get("create_user_if_missing") is True for p in open_session_payloads)
    # PR #5/#6 (2026-06-10): create_user_if_missing fast-path now lands at
    # the business-email step (NOT consent_cr); business_email_send fires
    # and parks waiting for the email.
    assert result.prompt == {"waiting_for": "email", "step": "business_email"}


async def test_email_new_lead_still_uses_complete_onboarding(make_harness) -> None:
    """The new-lead Email path keeps the legacy complete_onboarding flow —
    create_user_if_missing only applies to WhatsApp."""
    # Force the check_contact result to "new" for the email path.
    harness = make_harness()
    runtime = harness.platform.runtime
    await runtime.start("onboarding", EMAIL, EMAIL_ID, input={"trigger": "campaign"})
    await runtime.resume(EMAIL, EMAIL_ID, message={"text": "YES"})

    identity_calls = [name for name, _ in harness.identity.calls]
    # Email new-lead still goes through complete_onboarding.
    assert "complete_onboarding" in identity_calls
    open_session_payloads = [
        kwargs for name, kwargs in harness.identity.calls if name == "open_session"
    ]
    # And none of the open_session calls set create_user_if_missing on email.
    assert all(not p.get("create_user_if_missing") for p in open_session_payloads)


async def test_whatsapp_existing_user_still_uses_normal_session(make_harness) -> None:
    """Existing WhatsApp users hit the regular channel_session_first path —
    create_user_if_missing only applies to new-lead WhatsApp."""
    harness = make_harness(known_phones={PHONE: "user_42"})
    runtime = harness.platform.runtime
    await runtime.start("onboarding", WA, PHONE, input={"trigger": "campaign"})
    await runtime.resume(WA, PHONE, message={"text": "YES"})

    open_session_payloads = [
        kwargs for name, kwargs in harness.identity.calls if name == "open_session"
    ]
    # Existing-user fast-path: create_user_if_missing is False (default).
    assert all(not p.get("create_user_if_missing") for p in open_session_payloads)
