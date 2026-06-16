"""UAT 2026-06-16: admin "waive off" path on the monetization fee.

When Madad admin marks the payment as waived in the portal, the backend
sets ``user.onboardingFeePaid=True`` and advances ``journeyStatus`` past
QUALIFIED — but does NOT fire ``payment.completed``. Before the fix the
run sat at ``payment_await`` forever.

Two complementary fixes now cover the case:
* Ishaan's commit ``1bb9786``: when a webhook lands at payment_await
  carrying a journey_status hint of ACCEPTED/OFFER_ACCEPTED/ACTIVATED,
  short-circuit to paid=True and route into the lender flow. No
  agent-side message — the backend already sent the SME its own
  consolidated "fee waived → forwarded to banks" notice.
* The complement here: the BACKGROUND POLLER fires
  ``{type: status_update, last_status_source: poll}`` with NO
  journey_status piggy-backed. Ishaan's hint check returns None, so we
  fall through to a defensive /me read that picks up either signal
  (the ``onboardingFeePaid`` flag OR a journey jump past QUALIFIED) and
  advances the run silently — same no-message policy as the webhook
  branch.
"""

from __future__ import annotations

from app.shared.workflow import Channel

WA = Channel.WHATSAPP
IDENTITY = "+97455500W01"


async def _drive_to_payment_await(harness):
    """Walk the spec-aligned flow up to the payment_await interrupt."""
    runtime = harness.platform.runtime
    doc = "ZHVtbXk="

    async def resume(message):
        return await runtime.resume(WA, IDENTITY, message=message)

    await runtime.start("onboarding", WA, IDENTITY, input={"trigger": "campaign"})
    await resume({"text": "YES"})
    await resume({"text": "biz@example.com"})
    await resume({"attachments": [{"filename": "CR.pdf", "content_base64": doc}]})
    await resume({"attachments": [{"filename": "Audited.pdf", "content_base64": doc}]})
    await resume({"event": "prequalification.completed", "madadScore": 78})
    await resume(
        {"attachments": [{"filename": "Establishment.pdf", "content_base64": doc}]}
    )
    harness.identity.journey_status = "QUALIFIED"
    return await resume(
        {"event": "madad_score.ready", "journey_status": "QUALIFIED"}
    )


async def _drive_to_payment_wait_await(harness, identity_suffix: str = "W02"):
    """Walk the flow up to payment_wait_await (BEFORE the payment chain)."""
    identity = f"+97455500{identity_suffix}"
    runtime = harness.platform.runtime
    doc = "ZHVtbXk="

    async def resume(message):
        return await runtime.resume(WA, identity, message=message)

    await runtime.start("onboarding", WA, identity, input={"trigger": "campaign"})
    await resume({"text": "YES"})
    await resume({"text": "biz@example.com"})
    await resume({"attachments": [{"filename": "CR.pdf", "content_base64": doc}]})
    await resume({"attachments": [{"filename": "Audited.pdf", "content_base64": doc}]})
    await resume({"event": "prequalification.completed", "madadScore": 78})
    return identity


async def test_qualified_waived_at_payment_wait_skips_payment_chain(harness) -> None:
    """UAT 2026-06-16 (afternoon, +919497191690 repro): the SME is
    parked at payment_wait_await (admin-gate before the payment chain).
    Backend fires ``qualified.waived`` instead of releasing the gate.
    Agent must advance silently to lender_status_poll without firing
    onboarding.payment.* templates and without calling the payment
    chain (no business_details_fetch / payment_create / payment_send_link)."""
    from app.services.workflow.dispatcher import translate_backend_event
    identity = await _drive_to_payment_wait_await(harness, "WV1")
    runtime = harness.platform.runtime
    # Templates fired during the drive (campaign, financials, account,
    # docs prompt) — capture baseline.
    pay_templates_before = [
        t for t in harness.messenger.templates()
        if t.startswith("onboarding.payment.")
    ]

    # qualified.waived → translate → resume.
    payload = translate_backend_event("qualified.waived", {})
    await runtime.resume(WA, identity, message=payload)

    # No agent-side payment message — backend owns the waiver notice.
    pay_templates_after = [
        t for t in harness.messenger.templates()
        if t.startswith("onboarding.payment.")
    ]
    assert pay_templates_after == pay_templates_before, (
        "Agent must stay silent on qualified.waived; "
        f"new payment templates: {set(pay_templates_after) - set(pay_templates_before)}"
    )
    # Payment chain (TESS link create+send) MUST NOT have run.
    payment_calls = [name for name, _ in harness.payments.calls]
    assert "create_monetization_payment" not in payment_calls
    assert "send_monetization_payment_link" not in payment_calls


async def test_offers_available_at_payment_wait_breaks_out_to_lender(harness) -> None:
    """UAT 2026-06-16 (afternoon): a Phase 1.a status webhook carrying
    journey_status=ACCEPTED lands at payment_wait_await. The new
    break-out short-circuits to paid=True + jumps to lender_status_poll
    so the offer cards render instead of the resume getting swallowed."""
    from app.services.workflow.dispatcher import translate_backend_event
    identity = await _drive_to_payment_wait_await(harness, "WV2")
    runtime = harness.platform.runtime
    harness.identity.journey_status = "ACCEPTED"
    harness.identity._users_by_phone[identity] = "user-wv2"  # type: ignore[union-attr]

    payload = translate_backend_event(
        "offers.available", {"offers": [{"lender": "Qatar Islamic Bank"}]},
    )
    await runtime.resume(WA, identity, message=payload)

    # Payment chain still must NOT have fired.
    payment_calls = [name for name, _ in harness.payments.calls]
    assert "create_monetization_payment" not in payment_calls


async def test_poll_wake_advances_when_backend_journey_jumped(make_harness) -> None:
    """The poller fires ``{type: status_update, last_status_source: poll}``
    with NO journey_status piggy-backed. Ishaan's hint check returns None,
    then the /me fallback reads ACCEPTED off the backend and advances
    silently from the payment step — no payment-confirmed template fires
    (backend already sent its own consolidated waive notice). Downstream
    lender-flow templates may fire as the run progresses; that's expected."""
    harness = make_harness()
    await _drive_to_payment_await(harness)

    # The admin flipped the user past QUALIFIED while the SME was parked
    # — backend now reports ACCEPTED from /me.
    harness.identity.journey_status = "ACCEPTED"

    runtime = harness.platform.runtime
    result = await runtime.resume(
        WA, IDENTITY,
        message={"type": "status_update", "last_status_source": "poll"},
    )

    # Payment-side messaging stays quiet — the SME doesn't get a duplicate
    # "payment received" or "fee waived" notice from the agent.
    assert "onboarding.payment.confirmed" not in harness.messenger.templates()
    # The run is no longer parked at the payment prompt.
    assert result.prompt != {"waiting_for": "payment", "step": "payment"}


async def test_poll_wake_advances_via_onboarding_fee_paid_flag(make_harness) -> None:
    """Belt-and-braces: even when journeyStatus is still QUALIFIED
    (e.g. backend hasn't fired the lender phase yet), the explicit
    ``onboardingFeePaid=true`` flag is treated as waived. Exercises the
    flag branch independently of the journey jump."""

    class _FeePaidIdentity(type(make_harness().identity)):
        async def me(self, *, access_token: str):  # type: ignore[override]
            self._record("me", access_token=access_token)
            return {
                "user": {
                    "journeyStatus": "QUALIFIED",
                    "onboardingFeePaid": True,
                },
            }

    harness = make_harness()
    harness.platform.workflow._identity = _FeePaidIdentity(  # type: ignore[union-attr]
        journey_status="QUALIFIED",
    )
    harness.identity = harness.platform.workflow._identity  # type: ignore[union-attr]
    await _drive_to_payment_await(harness)

    runtime = harness.platform.runtime
    result = await runtime.resume(
        WA, IDENTITY,
        message={"type": "status_update", "last_status_source": "poll"},
    )

    # No payment-confirmed template from the agent.
    assert "onboarding.payment.confirmed" not in harness.messenger.templates()
    # The run advanced — no longer parked at payment.
    assert result.prompt != {"waiting_for": "payment", "step": "payment"}


async def test_real_payment_still_uses_confirmed_template(make_harness) -> None:
    """Regression guard: when the SME actually pays (paid=True from
    ``payment.completed``), the regular ``onboarding.payment.confirmed``
    template fires — the waiver short-circuits don't preempt it."""
    harness = make_harness()
    await _drive_to_payment_await(harness)

    runtime = harness.platform.runtime
    await runtime.resume(
        WA, IDENTITY, message={"type": "payment", "paid": True},
    )

    assert "onboarding.payment.confirmed" in harness.messenger.templates()


async def test_status_update_without_advance_keeps_run_parked(make_harness) -> None:
    """If the status_update wake fires but the backend still shows
    QUALIFIED and onboardingFeePaid=false, the run stays parked at
    payment_await — we don't waive on noise."""
    harness = make_harness()
    await _drive_to_payment_await(harness)
    template_count_before = len(harness.messenger.templates())
    # Backend hasn't moved; me() returns QUALIFIED + unpaid (the InMemory
    # default doesn't include onboardingFeePaid so it's falsy).
    harness.identity.journey_status = "QUALIFIED"

    runtime = harness.platform.runtime
    result = await runtime.resume(
        WA, IDENTITY,
        message={"type": "status_update", "last_status_source": "poll"},
    )

    assert len(harness.messenger.templates()) == template_count_before
    # Still parked at payment.
    assert result.prompt == {"waiting_for": "payment", "step": "payment"}
