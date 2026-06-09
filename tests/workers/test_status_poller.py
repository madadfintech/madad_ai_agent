"""Journey-status polling worker — cadence math + scan-and-resume behaviour."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from app.services.workflow import (
    InMemoryKycClient,
    InMemoryMadadIdentityClient,
    InMemoryMonetizationPaymentClient,
    RecordingMessenger,
    RecordingReminders,
    build_onboarding_platform,
)
from app.services.workflow.state import JourneyStatus
from app.shared.workflow import Channel
from app.shared.workflow.enums import RunStatus
from app.workers.status_poller import (
    CADENCE_FAST,
    CADENCE_MEDIUM,
    CADENCE_SLOW,
    cadence_for,
    poll_due,
    run_status_poller,
)

WA = Channel.WHATSAPP
NOW = datetime(2026, 6, 5, 12, 0, 0, tzinfo=UTC)


# -- Cadence per journey-status group ---------------------------------------


def test_cadence_15_min_for_pre_qualified_and_qualified() -> None:
    assert cadence_for(JourneyStatus.PRE_QUALIFIED) == CADENCE_MEDIUM
    assert cadence_for(JourneyStatus.QUALIFIED) == CADENCE_MEDIUM


def test_cadence_5_min_for_eligible() -> None:
    assert cadence_for(JourneyStatus.ELIGIBLE) == CADENCE_FAST


def test_cadence_1h_default_for_other_statuses() -> None:
    assert cadence_for(JourneyStatus.ONBOARDED) == CADENCE_SLOW
    assert cadence_for(JourneyStatus.UNVERIFIED) == CADENCE_SLOW
    assert cadence_for(None) == CADENCE_SLOW


# -- poll_due decision matrix ------------------------------------------------


def test_due_when_never_polled() -> None:
    assert (
        poll_due(
            last_polled_at=None,
            last_status_source=None,
            now=NOW,
            cadence=CADENCE_MEDIUM,
        )
        is True
    )


def test_not_due_when_elapsed_under_cadence() -> None:
    assert (
        poll_due(
            last_polled_at=NOW - timedelta(minutes=5),
            last_status_source="poll",
            now=NOW,
            cadence=CADENCE_MEDIUM,
        )
        is False
    )


def test_due_when_elapsed_over_cadence_with_poll_source() -> None:
    assert (
        poll_due(
            last_polled_at=NOW - timedelta(minutes=16),
            last_status_source="poll",
            now=NOW,
            cadence=CADENCE_MEDIUM,
        )
        is True
    )


def test_skip_one_cycle_after_webhook_within_2x_cadence() -> None:
    # Webhook fired 16 min ago (≥ cadence); still suppress (< 2× cadence).
    assert (
        poll_due(
            last_polled_at=NOW - timedelta(minutes=16),
            last_status_source="webhook",
            now=NOW,
            cadence=CADENCE_MEDIUM,
        )
        is False
    )


def test_due_again_after_2x_cadence_since_webhook() -> None:
    # Past two cadence-windows since webhook → resume normal polling.
    assert (
        poll_due(
            last_polled_at=NOW - timedelta(minutes=31),
            last_status_source="webhook",
            now=NOW,
            cadence=CADENCE_MEDIUM,
        )
        is True
    )


# -- run_status_poller — end-to-end on the platform --------------------------


def _build_platform(*, journey_status: str = "PRE_QUALIFIED"):
    return build_onboarding_platform(
        messenger=RecordingMessenger(),
        identity=InMemoryMadadIdentityClient(journey_status=journey_status),
        kyc=InMemoryKycClient(required_documents=["trade_license", "tax_card"]),
        payments=InMemoryMonetizationPaymentClient(),
        reminders=RecordingReminders(),
    )


async def _drive_to_journey_wait(platform, identity: str) -> None:
    """Drive the spec-aligned flow all the way to lender_wait_await, where the
    status_poll_on_demand has fired and set ``last_polled_at``. This is the
    poller's valid scope post-merge (payment-wait doesn't poll)."""
    runtime = platform.runtime
    doc = "ZHVtbXk="
    await runtime.start("onboarding", WA, identity, input={"trigger": "campaign"})

    async def resume(msg):
        return await runtime.resume(WA, identity, message=msg)

    await resume({"text": "YES"})
    await resume({"attachments": [{"filename": "CR.pdf", "content_base64": doc}]})
    await resume({"attachments": [{"filename": "Audited.pdf", "content_base64": doc}]})
    await resume({"event": "prequalification.completed", "madadScore": 78})
    # Bug #10a (2026-06-09): docs loop is strict — one valid upload +
    # admin-webhook exit, then madad_score.ready triggers payment.
    await resume(
        {"attachments": [{"filename": "Establishment_Card.pdf", "content_base64": doc}]}
    )
    await resume({"event": "documents.completed", "journey_status": "QUALIFIED"})
    platform.workflow._identity.journey_status = "QUALIFIED"  # type: ignore[union-attr]
    await resume({"event": "madad_score.ready", "journey_status": "QUALIFIED"})
    await resume({"type": "payment", "paid": True})


async def test_poller_skips_runs_not_at_polling_step() -> None:
    """A run still at the inbound-await steps shouldn't be touched by the
    poller — only journey_wait_await / lender_wait_await are pollable."""

    platform = _build_platform()
    runtime = platform.runtime

    identity = "+97455500801"
    await runtime.start("onboarding", WA, identity, input={"trigger": "campaign"})
    # Still at campaign_await — not a polling step.

    stats = await run_status_poller(platform, now=NOW)

    assert stats.polled == 0
    assert stats.skipped_step == 1


async def test_poller_advances_a_due_run_at_journey_wait_await() -> None:
    platform = _build_platform(journey_status="ELIGIBLE")
    identity = "+97455500802"
    await _drive_to_journey_wait(platform, identity)

    # Anchor "now" relative to the actual last_polled_at the workflow
    # recorded — synthetic NOW constants drift over time and msgpack
    # deserialization can return journey_status as an opaque type that
    # falls through cadence_for to CADENCE_SLOW (1h), so we need to be
    # well past one slow cadence to guarantee due.
    runs = await platform.runtime.run_store.list_by_status(
        RunStatus.WAITING_FOR_INPUT
    )
    run = next(r for r in runs if r.identity == identity)
    compiled = platform.runtime.loader.load(run.workflow, run.version)
    snap = await compiled.graph.aget_state(
        {"configurable": {"thread_id": run.thread_id}}
    )
    last_polled_at = snap.values["last_polled_at"]

    # Backend status advances → poller picks it up.
    # Post-merge the run is parked at lender_wait_await (we already paid).
    # An ACCEPTED status moves it to offers_fetch → offer_handoff terminal.
    platform.workflow._identity.journey_status = "ACCEPTED"  # type: ignore[union-attr]

    stats = await run_status_poller(
        platform, now=last_polled_at + timedelta(hours=2)
    )

    assert stats.polled == 1
    # After the poll, the run completed via offers_fetch → offer_handoff.
    runs = await platform.runtime.run_store.list_by_status(RunStatus.COMPLETED)
    matching = [r for r in runs if r.identity == identity]
    assert len(matching) == 1


async def test_poller_respects_webhook_suppression_window() -> None:
    """After a webhook arrives, the run's state shows
    last_status_source="webhook". A poller tick within one cadence-window
    after the webhook should skip; a tick well past two cadence-windows
    should poll.

    Anchors the comparisons against the actual last_polled_at the workflow
    recorded (wall clock) rather than a synthetic NOW so we don't drift.

    Uses ``transaction.disbursed`` (a Phase 1.b event without a
    journey_status hint in EVENT_TO_JOURNEY_STATUS) so the resume doesn't
    advance the run past journey_wait_await — the test is about cadence,
    not about routing.
    """

    platform = _build_platform(journey_status="ELIGIBLE")
    identity = "+97455500803"
    await _drive_to_journey_wait(platform, identity)

    await platform.dispatcher.on_backend_event(
        event_type="transaction.disbursed",
        event_id="evt-poller-1",
        channel=WA,
        identity=identity,
        payload={},
    )

    runs = await platform.runtime.run_store.list_by_status(
        RunStatus.WAITING_FOR_INPUT
    )
    run = next(r for r in runs if r.identity == identity)
    # Post-merge the run parks at lender_wait_await after payment.
    assert run.current_step == "lender_wait_await"

    compiled = platform.runtime.loader.load(run.workflow, run.version)
    snap = await compiled.graph.aget_state(
        {"configurable": {"thread_id": run.thread_id}}
    )
    last_polled_at = snap.values["last_polled_at"]
    assert snap.values["last_status_source"] == "webhook"

    # Post-merge the journey_status at lender_wait_await is PRE_QUALIFIED (set
    # by the score event), giving cadence_for(PRE_QUALIFIED) = CADENCE_MEDIUM
    # = 15 min. Tick at +16 min → past cadence but inside 2× cadence →
    # suppression engaged → skip.
    stats = await run_status_poller(
        platform, now=last_polled_at + timedelta(minutes=16)
    )
    assert stats.skipped_cadence == 1
    assert stats.polled == 0

    # Tick well past 2× cadence → suppression releases → poll.
    platform.workflow._identity.journey_status = "ACCEPTED"  # type: ignore[union-attr]
    stats2 = await run_status_poller(
        platform, now=last_polled_at + timedelta(minutes=45)
    )
    assert stats2.polled == 1
