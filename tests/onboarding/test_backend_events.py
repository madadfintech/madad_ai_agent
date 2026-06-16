"""Phase 4 dispatcher chokepoint: on_backend_event + WebhookDedupe +
translate_backend_event. Pin the contract: validation, dedupe, payload
translation."""

from __future__ import annotations

import pytest

from app.services.workflow import (
    ALL_BACKEND_EVENTS,
    PHASE1A_BACKEND_EVENTS,
    PHASE1B_BACKEND_EVENTS,
    InMemoryWebhookDedupe,
    UnknownEventTypeError,
    translate_backend_event,
)
from app.shared.workflow import Channel

WA = Channel.WHATSAPP
IDENTITY = "+97455500900"


# -- Static contract: event taxonomy ----------------------------------------


def test_phase1a_event_set_has_nine_canonical_events() -> None:
    # UAT 2026-06-16 Bug #8: backend fires ``offer.selected`` (not
    # ``offer.accepted``) at acceptance time. Accept both names so the
    # dispatcher never rejects the legit acceptance webhook.
    assert PHASE1A_BACKEND_EVENTS == frozenset(
        {
            "eligibility.updated",
            "documents.completed",
            "prequalification.completed",
            "madad_score.ready",
            "payment.completed",
            "offers.available",
            "offer.accepted",
            "offer.selected",
            "credit_line.activated",
        }
    )


def test_offer_selected_maps_to_offer_accepted_journey_status() -> None:
    """``offer.selected`` and ``offer.accepted`` both produce the same
    OFFER_ACCEPTED journey status so downstream routing is identical."""
    selected = translate_backend_event("offer.selected", {})
    accepted = translate_backend_event("offer.accepted", {})
    assert selected["journey_status"] == "OFFER_ACCEPTED"
    assert accepted["journey_status"] == "OFFER_ACCEPTED"


def test_phase1b_event_set_has_six_canonical_events() -> None:
    assert PHASE1B_BACKEND_EVENTS == frozenset(
        {
            "transaction.disbursed",
            "repayment.received",
            "repayment.partially_paid",
            "repayment.closed",
            "repayment.due_soon",
            "repayment.overdue",
        }
    )


def test_all_backend_events_is_union_of_phase1a_and_phase1b() -> None:
    assert ALL_BACKEND_EVENTS == PHASE1A_BACKEND_EVENTS | PHASE1B_BACKEND_EVENTS
    assert len(ALL_BACKEND_EVENTS) == 15  # 9 + 6 (Phase 1.a now includes offer.selected)


# -- Payload translation ----------------------------------------------------


def test_payment_completed_translates_to_paid_true() -> None:
    out = translate_backend_event(
        "payment.completed", {"payment_id": "pay-1", "amount_qar": 6000}
    )
    assert out["type"] == "payment"
    assert out["paid"] is True
    assert out["last_status_source"] == "webhook"
    assert out["payment_id"] == "pay-1"
    assert out["amount_qar"] == 6000


def test_non_payment_events_translate_to_status_update() -> None:
    out = translate_backend_event(
        "eligibility.updated", {"journey_status": "PRE_QUALIFIED"}
    )
    assert out["type"] == "status_update"
    assert out["event"] == "eligibility.updated"
    assert out["last_status_source"] == "webhook"
    assert out["journey_status"] == "PRE_QUALIFIED"


def test_translation_marks_source_as_webhook_so_poller_can_suppress() -> None:
    for event_type in PHASE1A_BACKEND_EVENTS:
        out = translate_backend_event(event_type, {})
        assert out["last_status_source"] == "webhook"


# -- Dispatcher chokepoint --------------------------------------------------


async def test_on_backend_event_rejects_unknown_event_type(harness) -> None:
    dispatcher = harness.platform.dispatcher

    with pytest.raises(UnknownEventTypeError) as exc_info:
        await dispatcher.on_backend_event(
            event_type="nonsense.event",
            event_id=None,
            channel=WA,
            identity=IDENTITY,
            payload={},
        )

    assert exc_info.value.event_type == "nonsense.event"


async def test_on_backend_event_dedupes_repeat_event_id(harness) -> None:
    runtime = harness.platform.runtime
    dispatcher = harness.platform.dispatcher

    await runtime.start("onboarding", WA, IDENTITY, input={"trigger": "campaign"})

    # First post advances the run; second post with the same event_id is dropped.
    first = await dispatcher.on_backend_event(
        event_type="eligibility.updated",
        event_id="evt-1",
        channel=WA,
        identity=IDENTITY,
        payload={"text": "anything"},
    )
    second = await dispatcher.on_backend_event(
        event_type="eligibility.updated",
        event_id="evt-1",
        channel=WA,
        identity=IDENTITY,
        payload={"text": "anything"},
    )

    assert first is not None
    assert second is None  # deduped — same event_id


async def test_on_backend_event_without_event_id_does_not_consult_dedupe(
    harness,
) -> None:
    """Backend may omit event_id (older catalogs); the receiver should
    forward without touching the dedupe layer (best-effort delivery).
    We assert by inspecting the InMemory dedupe's seen set after the call."""

    runtime = harness.platform.runtime
    dispatcher = harness.platform.dispatcher
    # The platform builds an InMemoryWebhookDedupe by default — read it back.
    dedupe = dispatcher._dedupe  # noqa: SLF001 — verifying internal seam
    assert isinstance(dedupe, InMemoryWebhookDedupe)

    await runtime.start("onboarding", WA, IDENTITY, input={"trigger": "campaign"})

    await dispatcher.on_backend_event(
        event_type="eligibility.updated",
        event_id=None,
        channel=WA,
        identity=IDENTITY,
        payload={},
    )

    assert dedupe.seen == set()  # never claimed — event_id was None


async def test_in_memory_dedupe_returns_false_on_repeat_claim() -> None:
    dedupe = InMemoryWebhookDedupe()
    assert await dedupe.claim("evt-1") is True
    assert await dedupe.claim("evt-1") is False  # already claimed
    assert await dedupe.claim("evt-2") is True   # different id, fresh


async def test_phase1b_event_accepted_but_no_workflow_handler_yet(harness) -> None:
    """Phase 1.b events (e.g. transaction.disbursed) are registered NOW so
    the receiver can already accept them; the matching workflow handlers
    land in Phase 6. The dispatcher should not raise — it forwards the
    payload to ``resume_external``; runs not awaiting it are unaffected."""

    runtime = harness.platform.runtime
    dispatcher = harness.platform.dispatcher

    await runtime.start("onboarding", WA, IDENTITY, input={"trigger": "campaign"})

    # Should not raise — Phase 1.b events are in ALL_BACKEND_EVENTS.
    result = await dispatcher.on_backend_event(
        event_type="transaction.disbursed",
        event_id="phase1b-evt-1",
        channel=WA,
        identity=IDENTITY,
        payload={},
    )

    # The campaign_await isn't a journey_status interrupt — the runtime will
    # treat the event as the awaited reply (text=None, attachments=[]) and
    # advance to declined (no YES). Verifying the dispatcher accepted the
    # event is what matters; the workflow's reaction is Phase 6.
    assert result is not None
