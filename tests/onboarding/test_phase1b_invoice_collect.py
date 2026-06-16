"""Phase 1.b — ``_invoice_collect_await`` post-activation behaviour.

The node has three routing paths:
1. Status query ("any update on my invoices?") → calls ``get_my_invoices``
   and renders the running status list.
2. ZIP attachment → ``submit_zip_base64`` (server-side recursive).
3. Single attachment → ``extract_and_submit_base64`` (preferred path).
"""

from __future__ import annotations

import base64
import io
import zipfile

from app.services.workflow import InMemoryInvoiceClient
from app.shared.workflow import Channel, RunStatus

WA = Channel.WHATSAPP
IDENTITY = "+97455500B01"
DOC = "ZHVtbXk="  # base64("dummy")


async def _drive_to_activated(harness) -> None:
    """Drive a happy-path run all the way through onboarding to the
    ``invoice_collect_await`` park point — the precondition for every
    Phase 1.b test."""
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
    await resume({"type": "status_update", "lenderName": "Qatar Islamic Bank"})
    harness.identity.journey_status = "ACTIVATED"
    await resume({"type": "status_update", "lenderName": "Qatar Islamic Bank"})


async def test_single_invoice_extract_then_confirm_then_submit(harness) -> None:
    """UAT 2026-06-16 #3: single-PDF flow is now extract-only → confirm
    card → submit on Approve. ``extract_and_submit_base64`` is reserved
    for the bulk/legacy paths."""
    await _drive_to_activated(harness)
    runtime = harness.platform.runtime

    # Step 1: SME sends the PDF — agent extracts only and shows confirm card.
    result = await runtime.resume(
        WA, IDENTITY,
        message={"attachments": [{"filename": "INV-1.pdf", "content_base64": DOC}]},
    )
    assert result.status == RunStatus.WAITING_FOR_INPUT
    assert result.prompt == {"waiting_for": "invoice", "step": "invoice_collect"}

    inv_calls_after_upload = [name for name, _ in harness.invoices.calls]
    assert inv_calls_after_upload == ["extract_base64"]
    assert "onboarding.invoice.confirm" in harness.messenger.templates()

    # Step 2: SME taps Approve — agent submits with the extracted fields.
    await runtime.resume(WA, IDENTITY, message={"text": "Approve"})

    inv_calls_after_approve = [name for name, _ in harness.invoices.calls]
    assert "submit_base64" in inv_calls_after_approve
    # ``onboarding.invoice.received`` confirms the submission to the SME.
    assert "onboarding.invoice.received" in harness.messenger.templates()
    # ZIP path didn't fire.
    assert "submit_zip_base64" not in inv_calls_after_approve


async def test_invoice_reject_discards_without_submitting(harness) -> None:
    """Reject button drops the draft — no submit_base64 call, no
    invoice_id appended to the ledger."""
    await _drive_to_activated(harness)
    runtime = harness.platform.runtime

    await runtime.resume(
        WA, IDENTITY,
        message={"attachments": [{"filename": "INV-1.pdf", "content_base64": DOC}]},
    )
    await runtime.resume(WA, IDENTITY, message={"text": "Reject"})

    inv_calls = [name for name, _ in harness.invoices.calls]
    assert "submit_base64" not in inv_calls
    assert "onboarding.invoice.rejected" in harness.messenger.templates()


async def test_invoice_edit_inline_updates_draft_and_reshows_card(harness) -> None:
    """``edit amount: 32000`` updates the draft and re-renders the
    confirm card with the new value."""
    await _drive_to_activated(harness)
    runtime = harness.platform.runtime

    await runtime.resume(
        WA, IDENTITY,
        message={"attachments": [{"filename": "INV-1.pdf", "content_base64": DOC}]},
    )
    confirm_count_before = harness.messenger.templates().count(
        "onboarding.invoice.confirm"
    )
    await runtime.resume(WA, IDENTITY, message={"text": "edit amount: 32000"})

    # Confirm card re-rendered (or its fallback content fired).
    assert (
        harness.messenger.templates().count("onboarding.invoice.confirm")
        > confirm_count_before
    )
    # And the new amount made it into the variables of the latest send.
    last_confirm = [
        s for s in harness.messenger.sent
        if s["template_key"] == "onboarding.invoice.confirm"
    ][-1]
    assert "32,000" in last_confirm["variables"]["amount"]

    # Approve now submits the EDITED amount.
    await runtime.resume(WA, IDENTITY, message={"text": "Approve"})
    submit_payloads = [
        payload for name, payload in harness.invoices.calls
        if name == "submit_base64"
    ]
    assert submit_payloads, "expected a submit_base64 call after Approve"
    assert str(submit_payloads[-1]["total_amount"]) == "32000"


async def test_zip_invoice_routes_through_bulk_preview(harness) -> None:
    """UAT 2026-06-16 #4: ZIP attachments now route through the local-
    unzip + per-member ``extract_base64`` preview path. ``submit_zip_base64``
    is no longer used for SME-WhatsApp uploads (kept on the port for
    completeness)."""
    await _drive_to_activated(harness)
    runtime = harness.platform.runtime

    # Build a real ZIP archive so `_is_zip_attachment` + the local
    # unzip helper recognise it.
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("inv1.pdf", b"dummy invoice 1")
        zf.writestr("inv2.pdf", b"dummy invoice 2")
    zip_b64 = base64.b64encode(buf.getvalue()).decode("ascii")

    await runtime.resume(
        WA, IDENTITY,
        message={
            "attachments": [
                {
                    "filename": "invoices.zip",
                    "content_base64": zip_b64,
                    "mime_type": "application/zip",
                }
            ]
        },
    )

    inv_calls = [name for name, _ in harness.invoices.calls]
    # Local-unzip then one extract_base64 per member (2 here).
    assert inv_calls.count("extract_base64") == 2
    assert "submit_zip_base64" not in inv_calls
    # The preview template fired.
    assert "onboarding.invoice.batch.preview" in harness.messenger.templates()


async def test_zip_batch_approve_all_submits_every_row(harness) -> None:
    """APPROVE ALL on a 2-row batch calls submit_base64 twice and
    appends both records to the ledger."""
    await _drive_to_activated(harness)
    runtime = harness.platform.runtime

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("inv1.pdf", b"x")
        zf.writestr("inv2.pdf", b"y")
    zip_b64 = base64.b64encode(buf.getvalue()).decode("ascii")

    await runtime.resume(WA, IDENTITY, message={
        "attachments": [{
            "filename": "invoices.zip",
            "content_base64": zip_b64,
            "mime_type": "application/zip",
        }],
    })
    # Now park at preview — SME taps "APPROVE ALL".
    await runtime.resume(WA, IDENTITY, message={"text": "APPROVE ALL"})

    inv_calls = [name for name, _ in harness.invoices.calls]
    assert inv_calls.count("submit_base64") == 2
    assert "onboarding.invoice.batch.submitted" in harness.messenger.templates()


async def test_zip_batch_remove_row_drops_one(harness) -> None:
    """REMOVE 1 drops the first row + re-renders the preview with 1 left."""
    await _drive_to_activated(harness)
    runtime = harness.platform.runtime

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("inv1.pdf", b"x")
        zf.writestr("inv2.pdf", b"y")
    zip_b64 = base64.b64encode(buf.getvalue()).decode("ascii")

    await runtime.resume(WA, IDENTITY, message={
        "attachments": [{
            "filename": "invoices.zip",
            "content_base64": zip_b64,
            "mime_type": "application/zip",
        }],
    })
    await runtime.resume(WA, IDENTITY, message={"text": "remove 1"})
    await runtime.resume(WA, IDENTITY, message={"text": "APPROVE ALL"})

    inv_calls = [name for name, _ in harness.invoices.calls]
    # Only one row left → only one submit.
    assert inv_calls.count("submit_base64") == 1


async def test_status_query_dispatches_get_my_invoices(harness) -> None:
    await _drive_to_activated(harness)
    runtime = harness.platform.runtime

    # First submit one so there's something to ask about.
    await runtime.resume(
        WA, IDENTITY,
        message={"attachments": [{"filename": "INV-1.pdf", "content_base64": DOC}]},
    )
    harness.invoices.calls.clear()  # focus the assertion on the status turn.

    await runtime.resume(WA, IDENTITY, message={"text": "any updates on my invoice?"})

    # Status path hit get_my_invoices, NOT extract_and_submit_base64.
    inv_calls = [name for name, _ in harness.invoices.calls]
    assert inv_calls == ["get_my_invoices"]
    assert "onboarding.invoice.status" in harness.messenger.templates()


async def test_off_script_chat_does_not_call_client(harness) -> None:
    """Idle chat at invoice_collect should be answered contextually
    without spuriously calling get_my_invoices / submit / extract."""
    await _drive_to_activated(harness)
    runtime = harness.platform.runtime
    harness.invoices.calls.clear()

    await runtime.resume(WA, IDENTITY, message={"text": "thanks for the help"})

    inv_calls = [name for name, _ in harness.invoices.calls]
    assert inv_calls == []


async def test_invoice_state_appends_per_submission(harness) -> None:
    """Each Approve adds one record to ``state.invoices_submitted`` —
    requires the new extract → confirm → Approve flow per #3."""
    await _drive_to_activated(harness)
    runtime = harness.platform.runtime

    # 1st invoice: extract → approve.
    await runtime.resume(
        WA, IDENTITY,
        message={"attachments": [{"filename": "INV-1.pdf", "content_base64": DOC}]},
    )
    await runtime.resume(WA, IDENTITY, message={"text": "Approve"})

    # 2nd invoice: extract → approve.
    await runtime.resume(
        WA, IDENTITY,
        message={"attachments": [{"filename": "INV-2.pdf", "content_base64": DOC}]},
    )
    await runtime.resume(WA, IDENTITY, message={"text": "Approve"})

    # Read state via the LangGraph checkpoint snapshot.
    session = await runtime.sessions.get(WA, IDENTITY)
    assert session is not None and session.active_run_id
    run = await runtime.run_store.get(session.active_run_id)
    compiled = runtime.loader.load(run.workflow, run.version)
    snap = await compiled.graph.aget_state(
        {"configurable": {"thread_id": run.thread_id}}
    )
    invoices = snap.values.get("invoices_submitted") or []
    assert len(invoices) == 2
    # Records carry the normalized shape — invoice_id, supplier, amount,
    # status — so downstream messages have a stable contract.
    record = invoices[0]
    for field in (
        "invoice_id",
        "supplier_name",
        "total_amount",
        "currency",
        "status",
        "filename",
    ):
        assert field in record


async def test_extract_failure_falls_back_to_invoice_failed_template(
    harness, monkeypatch
) -> None:
    """If the backend extraction raises (e.g. unreadable PDF), the SME
    sees the ``onboarding.invoice.failed`` template — not a silent run."""
    await _drive_to_activated(harness)

    # Swap in a client whose extract raises.
    failing = InMemoryInvoiceClient(extract_error=RuntimeError("backend unhappy"))
    harness.platform.workflow._invoices = failing  # type: ignore[union-attr]
    harness.invoices = failing

    runtime = harness.platform.runtime
    await runtime.resume(
        WA, IDENTITY,
        message={"attachments": [{"filename": "INV-bad.pdf", "content_base64": DOC}]},
    )

    assert "onboarding.invoice.failed" in harness.messenger.templates()
    # Backend-style error means "blame the file" copy — not a transport
    # timeout, so the SME is told to resend.
    sent_failures = [
        s for s in harness.messenger.sent
        if s["template_key"] == "onboarding.invoice.failed"
    ]
    assert sent_failures
    assert "couldn't read the file" in sent_failures[-1]["variables"]["reason"]


async def test_invoice_extract_timeout_uses_transport_message(harness) -> None:
    """UAT 2026-06-16: when the MCP cluster times out on
    extract_and_submit (NOT a real "unreadable" backend error), the SME
    must NOT be told the file is bad. They get the honest "our processor
    is slow, try again" copy so they don't waste time resending a
    perfectly valid PDF."""
    await _drive_to_activated(harness)

    timeout_client = InMemoryInvoiceClient(
        extract_error=TimeoutError("Timed out while waiting for response"),
    )
    harness.platform.workflow._invoices = timeout_client  # type: ignore[union-attr]
    harness.invoices = timeout_client

    runtime = harness.platform.runtime
    await runtime.resume(
        WA, IDENTITY,
        message={"attachments": [
            {"filename": "8044-invoice.pdf", "content_base64": DOC}
        ]},
    )

    sent_failures = [
        s for s in harness.messenger.sent
        if s["template_key"] == "onboarding.invoice.failed"
    ]
    assert sent_failures, "expected onboarding.invoice.failed to fire"
    reason = sent_failures[-1]["variables"]["reason"]
    assert "taking longer than usual" in reason
    # And critically: the SME is NOT told the file is corrupt.
    assert "couldn't read" not in reason


async def test_status_update_resume_at_invoice_collect_is_silent(harness) -> None:
    """UAT 2026-06-16 Bug #2 (+918287611995): backend Phase 1.a webhooks
    (offer.selected, credit_line.activated, etc.) and synthetic resumes
    (status poller, docs-settle sweep) legitimately land on the parked
    post-activation run. Each one used to drop into _smart_contextual
    and fire the canned "Whenever you have an invoice" prompt — 7 sends
    in 6 minutes for one SME. The node must re-park silently for these,
    and only respond to genuine SME-side text or attachments."""
    await _drive_to_activated(harness)
    template_count_before = len(harness.messenger.templates())

    runtime = harness.platform.runtime
    # Status-update from poller — no SME content.
    await runtime.resume(
        WA, IDENTITY,
        message={"type": "status_update", "last_status_source": "poll"},
    )
    # Status-update from a backend Phase 1.a webhook.
    await runtime.resume(
        WA, IDENTITY,
        message={
            "type": "status_update",
            "event": "offer.selected",
            "journey_status": "OFFER_ACCEPTED",
            "last_status_source": "webhook",
        },
    )
    # Docs-settle sweep (shouldn't happen here but defensive).
    await runtime.resume(WA, IDENTITY, message={"type": "docs_settle"})

    # All three were silent — no new template fired.
    assert len(harness.messenger.templates()) == template_count_before


async def test_qa_limit_answers_from_me_credit_line(harness, monkeypatch) -> None:
    """UAT 2026-06-16 #9: "what's my limit?" reads /me's creditLine and
    renders an approved/available answer with the real number."""
    await _drive_to_activated(harness)

    # Once parked at invoice_collect_await, return the creditLine shape
    # from /me. We swap AFTER the drive so the drive itself uses the
    # default me() that returns the journey status.
    original_me = harness.identity.me

    async def fake_me(*, access_token: str):
        await original_me(access_token=access_token)  # keep call-log
        return {
            "user": {
                "creditLine": {
                    "creditLimit": 100000,
                    "availableLimit": 75000,
                    "currency": "QAR",
                },
            },
        }
    monkeypatch.setattr(harness.identity, "me", fake_me)

    runtime = harness.platform.runtime
    await runtime.resume(WA, IDENTITY, message={"text": "what's my limit?"})

    sends = [
        s for s in harness.messenger.sent
        if s["template_key"] == "onboarding.help.contextual"
    ]
    assert sends, "expected help.contextual to fire for the limit Q&A"
    last_answer = sends[-1]["variables"]["answer"]
    assert "100,000" in last_answer
    assert "75,000" in last_answer


async def test_qa_disbursed_total_sums_state_ledger(harness) -> None:
    """UAT 2026-06-16 #9: "total disbursed?" sums the in-state ledger
    populated by transaction.disbursed webhooks."""
    await _drive_to_activated(harness)
    runtime = harness.platform.runtime

    # Fire two disbursement webhooks.
    for amt, ref in [(10000, "INV-1"), (7500, "INV-2")]:
        await runtime.resume(WA, IDENTITY, message={
            "type": "phase1b_event",
            "event": "transaction.disbursed",
            "last_status_source": "webhook",
            "invoiceNumber": ref,
            "disbursedAmount": amt,
            "currency": "QAR",
        })

    # Now ask.
    await runtime.resume(WA, IDENTITY, message={"text": "how much disbursed so far?"})

    sends = [
        s for s in harness.messenger.sent
        if s["template_key"] == "onboarding.help.contextual"
    ]
    last_answer = sends[-1]["variables"]["answer"]
    assert "17,500" in last_answer


async def test_qa_due_uses_repayment_outstanding_and_emis(harness) -> None:
    """UAT 2026-06-16 #9: "what's due?" reports outstanding + EMIs
    remaining from the latest repayment.received webhook record."""
    await _drive_to_activated(harness)
    runtime = harness.platform.runtime

    # Fire a repayment with new payload fields.
    await runtime.resume(WA, IDENTITY, message={
        "type": "phase1b_event",
        "event": "repayment.received",
        "last_status_source": "webhook",
        "invoiceNumber": "INV-77",
        "amount": 5000,
        "outstandingAmount": 25000,
        "emisTotal": 4, "emisPaid": 1, "emisRemaining": 3,
        "currency": "QAR",
        "closed": False,
    })

    await runtime.resume(WA, IDENTITY, message={"text": "what's due now?"})

    sends = [
        s for s in harness.messenger.sent
        if s["template_key"] == "onboarding.help.contextual"
    ]
    last_answer = sends[-1]["variables"]["answer"]
    assert "25,000" in last_answer
    assert "3 EMIs" in last_answer


async def test_genuine_text_still_triggers_contextual(harness) -> None:
    """Regression guard: real SME text input (not a synthetic resume)
    still produces the contextual answer."""
    await _drive_to_activated(harness)
    template_count_before = len(harness.messenger.templates())

    runtime = harness.platform.runtime
    await runtime.resume(WA, IDENTITY, message={"text": "hello what now?"})

    # The SME asked a real question — contextual answer SHOULD fire.
    assert len(harness.messenger.templates()) > template_count_before
    assert "onboarding.help.contextual" in harness.messenger.templates()


def test_is_invoice_status_query_recognizes_common_phrasings() -> None:
    from app.services.workflow.onboarding import _is_invoice_status_query

    assert _is_invoice_status_query({"text": "any update on my invoice?"})
    assert _is_invoice_status_query({"text": "where are my invoices"})
    assert _is_invoice_status_query({"text": "invoice status"})
    assert _is_invoice_status_query({"text": "when will it disburse"})
    assert _is_invoice_status_query({"text": "list my invoices"})
    # Negative cases.
    assert not _is_invoice_status_query({"text": ""})
    assert not _is_invoice_status_query({"text": "ok thanks"})
    # Attachments aren't status queries.
    assert not _is_invoice_status_query({"attachments": [{"filename": "x.pdf"}]})


def test_format_invoice_status_handles_empty_and_populated() -> None:
    from app.services.workflow.onboarding import _format_invoice_status_summary

    empty = _format_invoice_status_summary([])
    assert "haven't submitted any invoices yet" in empty

    populated = _format_invoice_status_summary([
        {
            "supplier_name": "Acme",
            "invoice_number": "INV-99",
            "total_amount": 500,
            "status": "DISBURSED",
        }
    ])
    assert "💸" in populated  # disbursed icon
    assert "Acme" in populated
    assert "DISBURSED" in populated
