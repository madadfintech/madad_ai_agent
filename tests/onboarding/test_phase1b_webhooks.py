"""Phase 1.b — disbursement + repayment webhook handlers fire from
``_invoice_collect_await``.

Each of the 6 events surfaces an SME-facing template and appends to the
local ledger. Asserts both the template used and that state accumulates
correctly so the dispatcher's chokepoint stays the single contract.
"""

from __future__ import annotations

import pytest

from app.shared.workflow import Channel

WA = Channel.WHATSAPP
IDENTITY = "+97455500B02"
DOC = "ZHVtbXk="


async def _drive_to_activated(harness) -> None:
    runtime = harness.platform.runtime

    async def resume(message):
        return await runtime.resume(WA, IDENTITY, message=message)

    await runtime.start("onboarding", WA, IDENTITY, input={"trigger": "campaign"})
    await resume({"text": "YES"})
    await resume({"text": "biz@example.com"})
    await resume({"attachments": [{"filename": "CR.pdf", "content_base64": DOC}]})
    await resume({"attachments": [{"filename": "Audited.pdf", "content_base64": DOC}]})
    await resume({"event": "prequalification.completed", "madadScore": 78})
    await resume(
        {"attachments": [{"filename": "Establishment_Card.pdf", "content_base64": DOC}]}
    )
    harness.identity.journey_status = "QUALIFIED"
    await resume({"event": "madad_score.ready", "journey_status": "QUALIFIED"})
    await resume({"type": "payment", "paid": True})
    harness.identity.journey_status = "ACCEPTED"
    await resume({"type": "status_update"})
    harness.identity.journey_status = "OFFER_ACCEPTED"
    await resume({"type": "status_update"})
    harness.identity.journey_status = "ACTIVATED"
    await resume({"type": "status_update"})


def _emit(event: str, payload: dict[str, object]) -> dict[str, object]:
    """The shape ``translate_backend_event`` produces for a Phase 1.b event —
    used so tests don't have to go through the dispatcher chokepoint to
    drive the workflow with a webhook."""
    return {
        "type": "phase1b_event",
        "event": event,
        "last_status_source": "webhook",
        **payload,
    }


@pytest.mark.parametrize("event,template", [
    ("transaction.disbursed", "onboarding.disbursement.received"),
    ("repayment.received", "onboarding.repayment.received"),
    ("repayment.partially_paid", "onboarding.repayment.partially_paid"),
    ("repayment.closed", "onboarding.repayment.closed"),
    ("repayment.due_soon", "onboarding.repayment.due_soon"),
    ("repayment.overdue", "onboarding.repayment.overdue"),
])
async def test_each_phase1b_event_fires_its_template(harness, event, template) -> None:
    await _drive_to_activated(harness)
    runtime = harness.platform.runtime

    await runtime.resume(
        WA, IDENTITY,
        message=_emit(event, {
            "amount": 5000, "currency": "QAR",
            "invoice_number": "INV-77",
            "outstanding": 0 if event == "repayment.closed" else 25000,
            "due_date": "2026-07-01",
            "days_overdue": 3,
        }),
    )

    assert template in harness.messenger.templates(), (
        f"{event} should fire {template}; got {harness.messenger.templates()}"
    )


async def test_disbursement_appends_to_state(harness) -> None:
    await _drive_to_activated(harness)
    runtime = harness.platform.runtime

    await runtime.resume(WA, IDENTITY, message=_emit("transaction.disbursed", {
        "amount": 10000, "currency": "QAR", "invoice_number": "INV-1",
    }))
    await runtime.resume(WA, IDENTITY, message=_emit("transaction.disbursed", {
        "amount": 7500, "currency": "QAR", "invoice_number": "INV-2",
    }))

    snap = await _snapshot(runtime)
    assert len(snap.values["disbursements_received"]) == 2
    assert snap.values["disbursements_received"][0]["amount"] == 10000
    assert snap.values["disbursements_received"][1]["invoice_ref"] == "INV-2"


async def test_repayment_updates_outstanding(harness) -> None:
    await _drive_to_activated(harness)
    runtime = harness.platform.runtime

    await runtime.resume(WA, IDENTITY, message=_emit("repayment.received", {
        "amount": 2000, "currency": "QAR",
        "invoice_number": "INV-9",
        "outstanding": 8000,
    }))
    snap = await _snapshot(runtime)
    assert snap.values["repayment_outstanding_qar"] == 8000

    await runtime.resume(WA, IDENTITY, message=_emit("repayment.closed", {
        "amount": 8000, "currency": "QAR",
        "invoice_number": "INV-9",
    }))
    snap = await _snapshot(runtime)
    assert snap.values["repayment_outstanding_qar"] == 0
    # Both repayment events recorded.
    assert len(snap.values["repayments_recorded"]) == 2


async def test_unknown_phase1b_event_is_dropped_silently(harness) -> None:
    """Defensive — a future Phase 1.b event arrives before we wire its
    template. The node must stay parked and not crash."""
    await _drive_to_activated(harness)
    runtime = harness.platform.runtime

    result = await runtime.resume(WA, IDENTITY, message=_emit(
        "transaction.something_new", {"amount": 0}
    ))
    # Either the prompt stays at invoice_collect, or the runtime returns
    # the parked state with no prompt set — both mean "didn't crash".
    if result.prompt is not None:
        assert result.prompt == {"waiting_for": "invoice", "step": "invoice_collect"}
    # No template fired for the unknown event.
    assert "onboarding.disbursement.received" not in harness.messenger.templates()


async def test_translate_marks_phase1b_event() -> None:
    """``translate_backend_event`` distinguishes Phase 1.b events from
    status_updates so ``_invoice_collect_await`` can branch reliably."""
    from app.services.workflow.dispatcher import translate_backend_event

    for event in (
        "transaction.disbursed",
        "repayment.received",
        "repayment.partially_paid",
        "repayment.closed",
        "repayment.due_soon",
        "repayment.overdue",
    ):
        payload = translate_backend_event(event, {"amount": 100})
        assert payload["type"] == "phase1b_event", event
        assert payload["event"] == event


async def _snapshot(runtime):
    session = await runtime.sessions.get(WA, IDENTITY)
    assert session is not None and session.active_run_id
    run = await runtime.run_store.get(session.active_run_id)
    compiled = runtime.loader.load(run.workflow, run.version)
    return await compiled.graph.aget_state(
        {"configurable": {"thread_id": run.thread_id}}
    )
