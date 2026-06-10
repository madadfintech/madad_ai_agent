"""Bug #2 + Bug #6 (closed 2026-06-10): returning users now route through
the new ``registered_route_send`` terminal instead of silently re-onboarding.

Driven by Ishan's ``madad_mcp_check_registration`` tool (cluster commit
e6ea5d2) — a read-only lookup that returns a ``route`` enum telling the
agent which message to send for a returning user. Seven canonical routes:
portal_login_required, invoice_discounting, offer_accepted_confirmation,
offers_available, payment_received, payment_link, continue_step.

These tests pin:
  * The fallthrough — when the cluster says ``registered=False``, the
    workflow uses the existing SIGN_UP / SIGN_UP-with-existing-user
    branches unchanged.
  * The routed-user path — when the cluster returns a route, the workflow
    sends the route-appropriate message via ``onboarding.help.contextual``
    and terminates with ``outcome="returning_user"``.
"""

from __future__ import annotations

import pytest

from app.shared.workflow import Channel, RunStatus

WA = Channel.WHATSAPP
IDENTITY = "+97455502601"


@pytest.mark.parametrize(
    "route, must_contain",
    [
        ("portal_login_required", "log in at uat-portal.madadfintech.com"),
        ("invoice_discounting", "credit line is already active"),
        ("offer_accepted_confirmation", "already accepted an offer"),
        ("offers_available", "financing offers are ready"),
        ("payment_received", "onboarding fee is already in"),
        ("payment_link", "complete the QAR 6,000 onboarding fee"),
        ("continue_step", "Picking up where you left off"),
    ],
)
async def test_returning_user_routes_terminate_with_correct_message(
    make_harness, route: str, must_contain: str
) -> None:
    """Each of the 7 routes Ishan's tool returns must produce a
    route-appropriate reply and complete the run without re-onboarding."""

    harness = make_harness(known_phones={IDENTITY: "user_42"})
    # Opt the InMemory client into the returning-user response for THIS
    # identity with THIS route. Other identities still get
    # ``registered=False`` so unrelated tests are unaffected.
    harness.identity.check_registration_overrides = {
        IDENTITY: {
            "route": route,
            "referenceNumber": "7388266",
            "journeyStatus": "QUALIFIED",
            "onboardingFeePaid": route in (
                "payment_received",
                "offers_available",
                "offer_accepted_confirmation",
                "invoice_discounting",
            ),
            "creditLineActive": route == "invoice_discounting",
            "offerAccepted": route == "offer_accepted_confirmation",
            "hasPendingOffers": route == "offers_available",
        }
    }

    runtime = harness.platform.runtime
    # Per Ishan (2026-06-10): cold-start entry_registration_check runs
    # BEFORE campaign_send, so a returning user's run terminates at
    # ``runtime.start`` itself — no YES needed.
    result = await runtime.start(
        "onboarding", WA, IDENTITY, input={"trigger": "campaign"}
    )

    # Returning-user runs terminate — no further onboarding work.
    assert result.status == RunStatus.COMPLETED, (
        f"route={route!r} should complete the run, got {result.status}"
    )
    assert result.values["outcome"] == "returning_user"
    # The contextual reply carries the route-specific answer.
    sent = [
        s for s in harness.messenger.sent
        if s["template_key"] == "onboarding.help.contextual"
    ]
    assert sent, f"contextual reply must fire for route={route!r}"
    answer = sent[-1]["variables"]["answer"]
    assert must_contain in answer, (
        f"route={route!r} answer should mention {must_contain!r}, got: {answer}"
    )


async def test_unregistered_user_falls_through_to_signup(make_harness) -> None:
    """``registered=False`` (the InMemory default) must leave the SIGN_UP
    path completely intact — no new node, no terminal short-circuit."""

    harness = make_harness()  # nobody is known
    runtime = harness.platform.runtime
    await runtime.start("onboarding", WA, IDENTITY, input={"trigger": "campaign"})
    after_yes = await runtime.resume(WA, IDENTITY, message={"text": "YES"})
    # PR #5/#6 (2026-06-10): new business-email step right after YES on
    # the SIGN_UP path. Confirms the fallthrough lands there (not at the
    # returning-user terminal).
    assert after_yes.prompt == {"waiting_for": "email", "step": "business_email"}
    result = await runtime.resume(
        WA, IDENTITY, message={"text": "biz@example.com"}
    )
    # And then the consent step as before.
    assert result.prompt == {"waiting_for": "upload", "step": "consent_cr"}
    assert result.values.get("outcome") != "returning_user"
    # check_registration WAS called (registry contract) — it just
    # returned registered=False so the router fell through.
    calls = [name for name, _ in harness.identity.calls]
    assert "check_registration" in calls


async def test_referenceNumber_threaded_into_state(make_harness) -> None:
    """When the registration payload carries a referenceNumber, the
    workflow promotes it onto ``state.application_ref`` so downstream
    nodes (status queries, payment-link re-sends) can use it."""

    harness = make_harness(known_phones={IDENTITY: "user_42"})
    harness.identity.check_registration_overrides = {
        IDENTITY: {"route": "payment_link", "referenceNumber": "7388266"},
    }
    runtime = harness.platform.runtime
    # Returning-user terminates at start (cold-start entry check).
    result = await runtime.start(
        "onboarding", WA, IDENTITY, input={"trigger": "campaign"}
    )

    assert result.values["application_ref"] == "7388266"
