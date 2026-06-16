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
    # UAT 2026-06-16 (PR #187): partially_paid + received both render the
    # repayment.received card now; the closed card is gated on the
    # ``closed=true`` flag in the payload.
    ("repayment.received", "onboarding.repayment.received"),
    ("repayment.partially_paid", "onboarding.repayment.received"),
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


async def test_repayment_received_with_closed_flag_fires_closed_template(harness) -> None:
    """Madad PR #187: backend fires ``repayment.received`` with
    ``closed=true`` when the last EMI clears. Agent must render the
    ``repayment.closed`` card (only event-name with no closed flag is
    ambiguous; the flag wins when both are present)."""
    await _drive_to_activated(harness)
    runtime = harness.platform.runtime

    await runtime.resume(
        WA, IDENTITY,
        message=_emit("repayment.received", {
            "invoiceNumber": "INV-LAST",
            "amount": 1000,
            "totalRepaid": 32000,
            "outstandingAmount": 0,
            "emisTotal": 4, "emisPaid": 4, "emisRemaining": 0,
            "paymasterName": "Qatar Pay",
            "lenderName": "Qatar Islamic Bank",
            "availableLimit": 100000,
            "currency": "QAR",
            "closed": True,
        }),
    )

    assert "onboarding.repayment.closed" in harness.messenger.templates()


async def test_disbursement_threads_utr_into_template(harness) -> None:
    """Madad PR #187: transaction.disbursed payload now includes
    ``utr`` + ``disbursedAmount`` + ``dueDate``. The template must
    receive all three substitutions."""
    await _drive_to_activated(harness)
    runtime = harness.platform.runtime

    await runtime.resume(
        WA, IDENTITY,
        message=_emit("transaction.disbursed", {
            "invoiceNumber": "INV-42",
            "disbursedAmount": 32000,
            "utr": "UTR-XYZ-12345",
            "dueDate": "2026-07-28",
            "currency": "QAR",
        }),
    )

    disb_sends = [
        s for s in harness.messenger.sent
        if s["template_key"] == "onboarding.disbursement.received"
    ]
    assert disb_sends, "expected disbursement template to fire"
    vars_ = disb_sends[-1]["variables"]
    assert vars_["utr"] == "UTR-XYZ-12345"
    assert "32,000" in vars_["amount"]
    assert vars_["ref"] == "INV-42"
    assert vars_["due_date"] == "2026-07-28"


async def test_repayment_received_threads_new_payload_fields(harness) -> None:
    """All 9 new payload fields from Madad PR #187 must reach the template."""
    await _drive_to_activated(harness)
    runtime = harness.platform.runtime

    await runtime.resume(
        WA, IDENTITY,
        message=_emit("repayment.received", {
            "invoiceNumber": "INV-7",
            "amount": 8000,
            "totalRepaid": 16000,
            "outstandingAmount": 24000,
            "emisTotal": 4, "emisPaid": 2, "emisRemaining": 2,
            "paymasterName": "Qatar Pay",
            "lenderName": "Qatar Islamic Bank",
            "availableLimit": 60000,
            "currency": "QAR",
            "dueDate": "2026-08-15",
            "closed": False,
        }),
    )

    sends = [
        s for s in harness.messenger.sent
        if s["template_key"] == "onboarding.repayment.received"
    ]
    assert sends, "expected repayment.received template to fire"
    vars_ = sends[-1]["variables"]
    assert "8,000" in vars_["amount"]
    assert "16,000" in vars_["total_repaid"]
    assert "24,000" in vars_["outstanding"]
    assert vars_["emis_paid"] == "2"
    assert vars_["emis_total"] == "4"
    assert vars_["emis_remaining"] == "2"
    assert vars_["paymaster"] == "Qatar Pay"
    assert vars_["lender"] == "Qatar Islamic Bank"
    assert vars_["due_date"] == "2026-08-15"


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


async def _drive_to_journey_wait(harness) -> None:
    """Drive a fresh run all the way to journey_wait_await (post-handoff)."""
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
        {"attachments": [{"filename": "Establishment.pdf", "content_base64": DOC}]}
    )
    harness.identity.journey_status = "QUALIFIED"
    await resume({"event": "madad_score.ready", "journey_status": "QUALIFIED"})
    await resume({"type": "payment", "paid": True})
    harness.identity.journey_status = "ACCEPTED"
    await resume({"type": "status_update"})
    harness.identity.journey_status = "OFFER_ACCEPTED"
    await resume({"type": "status_update"})


async def test_disbursement_at_journey_wait_still_updates_ledger(harness) -> None:
    """UAT 2026-06-16 (PM audit P0): a Phase 1.b webhook misrouted to
    a non-invoice wait node used to be silently dropped, losing the
    disbursement record. The new safety seam ensures the ledger AND
    SME-facing template still fire from journey_wait_await without
    forcing the run to jump to invoice_collect_await."""
    await _drive_to_journey_wait(harness)
    runtime = harness.platform.runtime
    # Confirm via the run record that we're at journey_wait_await.
    session = await runtime.sessions.get(WA, IDENTITY)
    assert session is not None and session.active_run_id
    run_before = await runtime.run_store.get(session.active_run_id)
    assert run_before.current_step == "journey_wait_await"

    await runtime.resume(
        WA, IDENTITY,
        message=_emit("transaction.disbursed", {
            "invoiceNumber": "INV-misrouted",
            "disbursedAmount": 25000,
            "utr": "UTR-9999",
            "currency": "QAR",
        }),
    )

    # Ledger updated even though we weren't at invoice_collect_await.
    snap_after = await _snapshot(runtime)
    disbursements = snap_after.values["disbursements_received"]
    assert len(disbursements) == 1
    assert disbursements[0]["invoice_ref"] == "INV-misrouted"
    assert disbursements[0]["utr"] == "UTR-9999"
    # Template fired (SME notified).
    assert "onboarding.disbursement.received" in harness.messenger.templates()
    # AND the run stayed at journey_wait_await — we didn't force a node jump.
    run_after = await runtime.run_store.get(session.active_run_id)
    assert run_after.current_step == "journey_wait_await"


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
