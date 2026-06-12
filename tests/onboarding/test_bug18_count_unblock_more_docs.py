"""Document-loop routing (rebuilt 2026-06-12).

Two behaviours pinned here:

1. Count-based unblock — ``_route_documents`` exits to ``complete`` when the
   cumulative attachment count meets the required count, even if some slots
   are still "pending" (classifier hangs, notably AoA). Mirrors the
   doc-service-level unblock in PR #4 (commit 6c05b1c).

2. NO "any more documents?" prompt (user 2026-06-12). That prompt caused a
   stuck loop — every non-YES/NO reply, INCLUDING an incoming qualify/offer
   webhook, re-fired the "No problem…" line and swallowed the status event so
   the payment message never came. Now: the coffee message fires exactly ONCE
   (``documents_complete_sent`` guard), the run re-parks in the upload-await
   node, and a QUALIFIED/ACCEPTED/OFFER_ACCEPTED/ACTIVATED status event ALWAYS
   routes straight to the payment/offer branch.
"""

from __future__ import annotations

from app.services.workflow.onboarding import (
    DEFAULT_WHATSAPP_REQUIRED_DOCS,
    OnboardingWorkflow,
)
from app.services.workflow.state import JourneyStatus, OnboardingState
from app.shared.workflow import Channel

WA = Channel.WHATSAPP
IDENTITY = "+97455501801"


def _wf() -> OnboardingWorkflow:
    from app.services.workflow.deps import build_onboarding_platform

    return build_onboarding_platform().workflow


def test_route_documents_unblocks_on_count_with_missing_slots() -> None:
    """Classifier hung on a few docs → required slots stay pending → the
    count-based unblock kicks in and lets the loop complete (coffee once)."""
    wf = _wf()
    state = OnboardingState(
        identity=IDENTITY,
        missing_documents=["aoa", "proof_of_address"],
        docs_uploaded_count=len(DEFAULT_WHATSAPP_REQUIRED_DOCS),
    )
    assert wf._route_documents(state) == "complete"  # noqa: SLF001


def test_route_documents_does_not_unblock_below_threshold() -> None:
    """1 upload vs 10 required must NOT unblock."""
    wf = _wf()
    state = OnboardingState(
        identity=IDENTITY,
        missing_documents=list(DEFAULT_WHATSAPP_REQUIRED_DOCS),
        docs_uploaded_count=1,
    )
    assert wf._route_documents(state) == "await_again"  # noqa: SLF001


def test_route_documents_natural_completion_first_time_shows_coffee() -> None:
    """Every required slot filled, coffee not yet sent → ``complete`` (so the
    coffee message fires)."""
    wf = _wf()
    state = OnboardingState(identity=IDENTITY, missing_documents=[])
    assert wf._route_documents(state) == "complete"  # noqa: SLF001


def test_route_documents_reparks_silently_after_coffee() -> None:
    """Once the coffee has been sent, a still-complete checklist must NOT
    re-show it — it re-parks silently in the upload-await node. This is the
    fix for the repeated coffee / 'any more documents?' spam."""
    wf = _wf()
    state = OnboardingState(
        identity=IDENTITY,
        missing_documents=[],
        documents_complete_sent=True,
    )
    assert wf._route_documents(state) == "await_again"  # noqa: SLF001


def test_route_documents_qualify_always_routes_to_payment() -> None:
    """A QUALIFIED/ACCEPTED/OFFER_ACCEPTED/ACTIVATED status ALWAYS routes to
    payment — even mid-docs, even after the coffee, even with docs missing.
    This guarantees the qualify/payment message is never swallowed by the
    document phase (the recurring stuck-message bug)."""
    wf = _wf()
    for status in (
        JourneyStatus.QUALIFIED,
        JourneyStatus.ACCEPTED,
        JourneyStatus.OFFER_ACCEPTED,
        JourneyStatus.ACTIVATED,
    ):
        state = OnboardingState(
            identity=IDENTITY,
            missing_documents=list(DEFAULT_WHATSAPP_REQUIRED_DOCS),  # docs not done
            documents_complete_sent=True,
            journey_status=status,
        )
        assert wf._route_documents(state) == "payment"  # noqa: SLF001
    # Same via the explicit payment_ready flag (set on the score.ready event).
    state = OnboardingState(identity=IDENTITY, payment_ready=True)
    assert wf._route_documents(state) == "payment"  # noqa: SLF001
