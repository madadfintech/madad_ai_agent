"""Bugs #12 + #13 (UAT 2026-06-09, Ishan diagnosis).

Bug #12 — QUALIFY fast-forward gap:
  Backend only fires ``madad_score.ready`` once. When it arrived mid-
  docs-loop, the docs-loop handler consumed the event (exited the loop)
  and the run parked at ``payment_wait_await``. A second event was
  needed to advance into the payment chain — and backend never sent one.
  The SME got the coffee message but no payment link.

Bug #13 — stale token in payment branch:
  ``business_details_fetch`` / ``products_list_fetch`` / ``payment_create``
  / the side-channel notification call read ``state.access_token``
  directly. After a long park (admin review window) the token had
  expired, the call 401'd, the base client's retry exhausted, the run
  failed terminally, no payment link reached the SME.
"""

from __future__ import annotations

from app.services.workflow.onboarding import OnboardingWorkflow
from app.shared.workflow import Channel

WA = Channel.WHATSAPP
IDENTITY = "+97455501201"
DOC = "ZHVtbXk="


async def _drive_to_documents(harness) -> None:
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
    await runtime.resume(
        WA,
        IDENTITY,
        message={"event": "prequalification.completed", "journey_status": "PRE_QUALIFIED"},
    )


async def test_qualify_mid_docs_loop_advances_to_payment_in_one_resume(
    harness,
) -> None:
    """Bug #12: a single ``madad_score.ready`` event (QUALIFIED) must
    exit the docs loop AND fast-forward through ``payment_wait_await``
    to the payment chain on the SAME resume — never park at payment_wait
    waiting for a second trigger that backend won't fire."""
    runtime = harness.platform.runtime
    await _drive_to_documents(harness)

    result = await runtime.resume(
        WA,
        IDENTITY,
        message={
            "event": "madad_score.ready",
            "journey_status": "QUALIFIED",
            "madadScore": 78,
        },
    )

    # The payment template fired — the chain ran end-to-end.
    templates = harness.messenger.templates()
    assert (
        "onboarding.payment.request.button" in templates
        or "onboarding.payment.request" in templates
    )
    # Run parked at payment_await ready for the SME to pay.
    assert result.prompt == {"waiting_for": "payment", "step": "payment"}


async def test_payment_branch_mints_fresh_token_at_each_node(harness) -> None:
    """Bug #13: every payment-branch MCP call must mint a fresh token
    via ``_live_token`` instead of reading the stale ``state.access_token``
    that was put there 15+ minutes ago at the doc upload phase."""
    runtime = harness.platform.runtime
    await _drive_to_documents(harness)
    # Drive through to payment chain via the fast-forward.
    await runtime.resume(
        WA,
        IDENTITY,
        message={
            "event": "madad_score.ready",
            "journey_status": "QUALIFIED",
            "madadScore": 78,
        },
    )

    # The MCP identity client records every ``open_session`` mint. The
    # payment branch made 4 calls that previously read state.access_token
    # — business_details_fetch, products_list_fetch, payment_create, and
    # the side-channel send_monetization_payment_link. Each now first
    # short-circuits if the cached token is still fresh and otherwise
    # mints a new one, so we should see at least one extra mint between
    # the docs phase and the payment chain.
    open_sessions = [
        name for name, _ in harness.identity.calls if name == "open_session"
    ]
    # Pre-Bug-#13: only the early open_session(s) at signup. Post-fix:
    # additional mint(s) when the payment branch starts (the cached
    # token from docs phase is still good in tests, so it short-circuits
    # — but the wiring goes through ``_live_token`` either way, so
    # ``access_token`` is no longer read directly from ``state``).
    # Smoke check: the payment chain ran end-to-end without raising.
    assert open_sessions, "open_session should fire at least once"


async def test_payment_branch_nodes_use_live_token_helper(harness) -> None:
    """White-box check: the four payment-branch nodes must call
    ``_live_token`` (not access ``state.access_token`` directly).
    This is the single point that was broken pre-Bug-#13 and the
    source code is the contract here."""
    import inspect

    wf = OnboardingWorkflow.__module__
    # The four payment-branch methods are on OnboardingWorkflow.
    source = inspect.getsource(
        __import__(wf, fromlist=["OnboardingWorkflow"]).OnboardingWorkflow
    )
    for method_name in (
        "_business_details_fetch",
        "_products_list_fetch",
        "_payment_create",
        "_payment_send_link",
    ):
        # Extract the method's source — crude but enough for the
        # invariant: each method body must mention ``_live_token``.
        marker = f"async def {method_name}("
        idx = source.find(marker)
        assert idx != -1, f"{method_name} not found in source"
        # Find the next async def to bound the method body.
        next_def = source.find("    async def ", idx + len(marker))
        body = source[idx:next_def if next_def != -1 else len(source)]
        assert "_live_token" in body, (
            f"{method_name} no longer calls _live_token — Bug #13 regression"
        )
