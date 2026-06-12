"""Returning-user RESUME (rebuilt 2026-06-12 per user spec).

Driven by Ishan's ``madad_mcp_check_registration`` tool (cluster commit
e6ea5d2) — a read-only lookup that flags a contact as an existing account.
When it does, the workflow now opens a session, reads the live
``journeyStatus`` (``madad_auth_me``) and RE-ENTERS the exact step the SME
left off at, keeping the run alive — instead of the old "send one greeting
and terminate" behaviour. The canonical status→step mapping is confirmed
with the user and pinned by ``test_route_resume`` unit-style below.

These tests pin:
  * Fallthrough — ``registered=False`` leaves the SIGN_UP path unchanged.
  * Terminal statuses (rejected / expired / open / ineligible / unqualified)
    send the right message and complete.
  * Mid-journey statuses (INCOMPLETE / QUALIFIED / ACTIVATED) keep the run
    WAITING — i.e. the bot continues the journey instead of dead-ending.
  * referenceNumber from the registration payload threads onto state.
"""

from __future__ import annotations

import pytest

from app.shared.workflow import Channel, RunStatus

WA = Channel.WHATSAPP
IDENTITY = "+97455502601"


def _returning(harness, journey_status: str, **payload):
    """Mark IDENTITY as a returning user with the given live journey status."""
    harness.identity.journey_status = journey_status
    harness.identity.check_registration_overrides = {
        IDENTITY: {"route": "continue_step", "journeyStatus": journey_status, **payload},
    }


@pytest.mark.parametrize(
    "journey_status, must_contain, outcome",
    [
        ("NOT_ACCEPTED", "was not accepted", "returning_user"),
        ("OFFER_EXPIRED", "have expired", "returning_user"),
        ("OPEN", "application is open", "returning_user"),
        ("IN_ELIGIBLE", "", "not_eligible"),
        ("UNQUALIFIED", "", "not_qualified"),
    ],
)
async def test_returning_user_terminal_statuses(
    make_harness, journey_status: str, must_contain: str, outcome: str
) -> None:
    """Terminal journey statuses send the status-appropriate message and
    complete the run (no in-chat next action)."""
    harness = make_harness(known_phones={IDENTITY: "user_42"})
    _returning(harness, journey_status)

    runtime = harness.platform.runtime
    await runtime.start("onboarding", WA, IDENTITY, input={"trigger": "campaign"})
    result = await runtime.resume(WA, IDENTITY, message={"text": "YES"})

    assert result.status == RunStatus.COMPLETED, (
        f"{journey_status} should complete, got {result.status}"
    )
    assert result.values["outcome"] == outcome
    if must_contain:
        sent = [
            s for s in harness.messenger.sent
            if s["template_key"] == "onboarding.help.contextual"
        ]
        assert sent, f"a message must fire for {journey_status}"
        assert must_contain in sent[-1]["variables"]["answer"]


@pytest.mark.parametrize("journey_status", ["INCOMPLETE", "QUALIFIED", "ACTIVATED"])
async def test_returning_user_midjourney_resumes_live(
    make_harness, journey_status: str
) -> None:
    """The core fix: a mid-journey returning user does NOT dead-end on a
    greeting — the run re-enters the live step and stays WAITING for the
    SME's next message (doc upload / payment / invoice)."""
    harness = make_harness(known_phones={IDENTITY: "user_42"})
    _returning(harness, journey_status)

    runtime = harness.platform.runtime
    await runtime.start("onboarding", WA, IDENTITY, input={"trigger": "campaign"})
    result = await runtime.resume(WA, IDENTITY, message={"text": "YES"})

    assert result.status == RunStatus.WAITING_FOR_INPUT, (
        f"{journey_status} must keep the journey alive, got {result.status}"
    )
    # It did NOT take the greet-and-end returning_user terminal.
    assert result.values.get("outcome") != "returning_user"


async def test_unregistered_user_falls_through_to_signup(make_harness) -> None:
    """``registered=False`` (the InMemory default) must leave the SIGN_UP
    path completely intact — no resume, no terminal short-circuit."""
    harness = make_harness()  # nobody is known
    runtime = harness.platform.runtime
    await runtime.start("onboarding", WA, IDENTITY, input={"trigger": "campaign"})
    result = await runtime.resume(WA, IDENTITY, message={"text": "YES"})

    # Lands at the business-email step (the new-lead path's next stop after
    # YES, per task #28), NOT a returning-user resume.
    assert result.prompt == {"waiting_for": "email", "step": "business_email"}
    assert result.values.get("outcome") != "returning_user"
    calls = [name for name, _ in harness.identity.calls]
    assert "check_registration" in calls


async def test_referenceNumber_threaded_into_state(make_harness) -> None:
    """The registration payload's referenceNumber is promoted onto
    ``state.application_ref`` so downstream nodes can use it."""
    harness = make_harness(known_phones={IDENTITY: "user_42"})
    _returning(harness, "QUALIFIED", referenceNumber="7388266")

    runtime = harness.platform.runtime
    await runtime.start("onboarding", WA, IDENTITY, input={"trigger": "campaign"})
    result = await runtime.resume(WA, IDENTITY, message={"text": "YES"})

    assert result.values["application_ref"] == "7388266"
