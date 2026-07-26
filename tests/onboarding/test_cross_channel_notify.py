"""Component C — cross-channel notify (user 2026-07-26).

Two behaviours once the SME has used BOTH channels:
  * milestone mirror — an application-progress milestone (e.g. pre-qualified) is
    delivered on BOTH WhatsApp and email (same message), not just the last-active
    one; and
  * switch-progress ping — when the SME switches channels, their ORIGIN channel is
    told the application continued elsewhere and is in sync.
If the SME used only one channel, nothing is mirrored/pinged (single-channel).
"""

from __future__ import annotations

from app.shared.workflow import Channel

WA = Channel.WHATSAPP
EMAIL = Channel.EMAIL
PHONE = "+97455501234"
EMAIL_ADDR = "sme@bizmail.qa"
CR = "Q1JfYnl0ZXNfYw=="
AUDIT = "QVVESVRfYnl0ZXM="


async def _drive_wa_to_financials(harness) -> None:
    runtime = harness.platform.runtime
    await runtime.start("onboarding", WA, PHONE, input={"trigger": "campaign"})
    await runtime.resume(WA, PHONE, message={"text": "YES"})
    await runtime.resume(WA, PHONE, message={"text": EMAIL_ADDR})  # business_email
    await runtime.resume(
        WA, PHONE, message={"attachments": [{"filename": "CR.pdf", "content_base64": CR}]}
    )


async def test_switch_ping_fires_to_home_channel(make_harness) -> None:
    harness = make_harness()
    msg = harness.messenger
    await _drive_wa_to_financials(harness)
    harness.identity.check_registration_overrides = {
        EMAIL_ADDR: {"registered": True, "phoneNumber": PHONE, "email": EMAIL_ADDR},
    }
    before = len(msg.sent)
    # SME switches to email and sends the audited report there.
    await harness.platform.dispatcher.inbound(
        EMAIL, EMAIL_ADDR,
        attachments=[{"filename": "Audit.pdf", "content_base64": AUDIT}],
    )
    # A switch-progress ping must have gone to the HOME (WhatsApp) channel.
    pings = [
        s for s in msg.sent[before:]
        if s["channel"] is WA
        and "in sync" in str((s.get("variables") or {}).get("answer", "")).lower()
    ]
    assert pings, "expected a switch-progress ping on WhatsApp after the email switch"
    # Customer-facing: the ping must NOT leak internal step terminology.
    for s in pings:
        ans = str((s.get("variables") or {}).get("answer", "")).lower()
        assert "financ" not in ans and "await" not in ans, f"ping leaked internal term: {ans}"


async def test_milestone_mirrors_to_both_channels_when_dual(make_harness) -> None:
    harness = make_harness()
    runtime = harness.platform.runtime
    msg = harness.messenger
    await _drive_wa_to_financials(harness)
    harness.identity.check_registration_overrides = {
        EMAIL_ADDR: {"registered": True, "phoneNumber": PHONE, "email": EMAIL_ADDR},
    }
    # Switch to email for the financials → 'email' joins channels_seen; the run
    # advances to account.created + prequalify wait.
    await harness.platform.dispatcher.inbound(
        EMAIL, EMAIL_ADDR,
        attachments=[{"filename": "Audit.pdf", "content_base64": AUDIT}],
    )
    before = len(msg.sent)
    # Fire the pre-qualification milestone (backend event).
    await harness.platform.dispatcher.on_backend_event(
        event_type="prequalification.completed",
        event_id=None,
        channel=WA,
        identity=PHONE,
        payload={"journey_status": "PRE_QUALIFIED"},
    )
    # The pre-qualified checklist milestone must reach BOTH channels.
    checklist = [
        s for s in msg.sent[before:]
        if s.get("template_key") == "onboarding.documents.checklist"
    ]
    channels = {s["channel"] for s in checklist}
    assert WA in channels and EMAIL in channels, (
        f"pre-qual milestone should mirror to both channels, got {channels}"
    )


async def test_single_channel_sme_no_mirror(make_harness) -> None:
    """A WhatsApp-only SME (never emailed) gets milestones on WhatsApp ONLY."""
    harness = make_harness()
    runtime = harness.platform.runtime
    msg = harness.messenger
    await _drive_wa_to_financials(harness)
    # Provide the audited report on WhatsApp too (no channel switch).
    await runtime.resume(
        WA, PHONE, message={"attachments": [{"filename": "Audit.pdf", "content_base64": AUDIT}]}
    )
    before = len(msg.sent)
    await harness.platform.dispatcher.on_backend_event(
        event_type="prequalification.completed",
        event_id=None,
        channel=WA,
        identity=PHONE,
        payload={"journey_status": "PRE_QUALIFIED"},
    )
    checklist = [
        s for s in msg.sent[before:]
        if s.get("template_key") == "onboarding.documents.checklist"
    ]
    channels = {s["channel"] for s in checklist}
    assert channels == {WA}, f"single-channel SME must not mirror, got {channels}"
