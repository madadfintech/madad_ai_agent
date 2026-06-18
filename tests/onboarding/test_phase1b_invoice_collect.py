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


async def test_single_invoice_submits_immediately_no_confirm_card(
    harness,
) -> None:
    """UAT 2026-06-18 (Ishan Bug 1) — SUBMIT-FIRST. Single PDF goes
    straight to ``extract_and_submit_base64`` (backend creates instantly,
    enriches via OCR async). NO confirm card, NO Approve/Edit/Reject UX.
    SME sees one "processing" ack then the "submitted ✅" ack."""
    await _drive_to_activated(harness)
    runtime = harness.platform.runtime

    result = await runtime.resume(
        WA, IDENTITY,
        message={"attachments": [{"filename": "INV-1.pdf", "content_base64": DOC}]},
    )
    assert result.status == RunStatus.WAITING_FOR_INPUT

    inv_calls = [name for name, _ in harness.invoices.calls]
    # The new flow calls extract_and_submit_base64 directly (no
    # extract_base64 + Approve handshake any more).
    assert "extract_and_submit_base64" in inv_calls
    assert "extract_base64" not in inv_calls
    assert "submit_base64" not in inv_calls

    templates = harness.messenger.templates()
    # New "submitted ✅" ack confirms the submission to the SME.
    assert "onboarding.invoice.submitted" in templates
    # No confirm card / Approve handshake on the new flow.
    assert "onboarding.invoice.confirm" not in templates


async def test_invoice_no_resend_prompt_on_partial_ocr(
    make_harness,
) -> None:
    """UAT 2026-06-18 (Ishan Bug 1): the "We couldn't read the file"
    resend prompt is GONE. Backend accepts blanks (invoiceNumber='N/A',
    totalAmount=0); ops fills the rest. Even if the backend response
    is sparse, the SME sees the "submitted ✅" ack — never a resend
    blocker."""
    from app.services.workflow import InMemoryInvoiceClient

    class _SparseClient(InMemoryInvoiceClient):
        async def extract_and_submit_base64(  # type: ignore[override]
            self, *, access_token, filename, content_base64, mime_type=None,
            user_id=None, status="UNVERIFIED",
        ):
            self._record(
                "extract_and_submit_base64", access_token=access_token,
                filename=filename, mime_type=mime_type, user_id=user_id,
                status=status,
            )
            # Backend creates with blanks — no supplier, no amount.
            return {
                "invoice_id": "inv-sparse-1", "filename": filename,
                "status": "SUBMITTED",
            }

    harness = make_harness()
    await _drive_to_activated(harness)
    harness.platform.workflow._invoices = _SparseClient()  # type: ignore[union-attr]
    harness.invoices = harness.platform.workflow._invoices  # type: ignore[union-attr]

    runtime = harness.platform.runtime
    await runtime.resume(
        WA, IDENTITY,
        message={"attachments": [{"filename": "blurry.pdf", "content_base64": DOC}]},
    )

    assert "onboarding.invoice.submitted" in harness.messenger.templates()
    failed_sends = [
        s for s in harness.messenger.sent
        if s["template_key"] == "onboarding.invoice.failed"
    ]
    assert not failed_sends, (
        "agent must NEVER block on partial OCR — backend accepts blanks "
        "and ops fills the rest manually"
    )


async def test_zip_invoice_submits_every_member_no_preview(harness) -> None:
    """UAT 2026-06-18 (Ishan QA) — bulk SUBMIT-FIRST. ZIP attachments
    now route directly through ``extract_and_submit_base64`` per member.
    No CSV preview, no APPROVE ALL handshake. Every member submits."""
    await _drive_to_activated(harness)
    runtime = harness.platform.runtime

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
    # Submit-first: extract_and_submit_base64 fires once per member.
    assert inv_calls.count("extract_and_submit_base64") == 2
    # The old extract-only / submit_zip paths are NOT used.
    assert "extract_base64" not in inv_calls
    assert "submit_zip_base64" not in inv_calls
    # No CSV preview / APPROVE ALL UX any more — one consolidated receipt.
    templates = harness.messenger.templates()
    assert "onboarding.invoice.batch.preview" not in templates
    assert "onboarding.invoice.bulk.submitted" in templates


async def test_zip_invoice_never_drops_member_on_failure(make_harness) -> None:
    """UAT 2026-06-18 (Ishan QA) RCA: the old bulk_preview DROPPED any
    member whose extract_base64 raised. With submit-first the SME's
    invoice must NEVER vanish silently — failed members are surfaced in
    the consolidated receipt's failure_block so they can resend only the
    affected ones."""
    from app.services.workflow import InMemoryInvoiceClient

    # Client that succeeds on the first member, fails on the second.
    call_count = {"n": 0}

    class _PartiallyFailingClient(InMemoryInvoiceClient):
        async def extract_and_submit_base64(  # type: ignore[override]
            self, *, access_token, filename, content_base64, mime_type=None,
            user_id=None, status="UNVERIFIED",
        ):
            call_count["n"] += 1
            self._record(
                "extract_and_submit_base64", access_token=access_token,
                filename=filename, mime_type=mime_type,
                user_id=user_id, status=status,
            )
            if call_count["n"] == 2:
                raise RuntimeError("backend hiccup on member 2")
            return {
                "invoice_id": f"inv-bulk-{call_count['n']}",
                "filename": filename,
                "status": status,
            }

    harness = make_harness()
    await _drive_to_activated(harness)
    harness.platform.workflow._invoices = _PartiallyFailingClient()  # type: ignore[union-attr]
    harness.invoices = harness.platform.workflow._invoices  # type: ignore[union-attr]
    runtime = harness.platform.runtime

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("good.pdf", b"x")
        zf.writestr("bad.pdf", b"y")
    zip_b64 = base64.b64encode(buf.getvalue()).decode("ascii")

    await runtime.resume(WA, IDENTITY, message={
        "attachments": [{
            "filename": "invoices.zip",
            "content_base64": zip_b64,
            "mime_type": "application/zip",
        }],
    })

    # Both members were ATTEMPTED — second failure must NOT cause silent drop.
    inv_calls = [name for name, _ in harness.invoices.calls]
    assert inv_calls.count("extract_and_submit_base64") == 2

    # Receipt fired AND mentions the failure (so SME can resend bad.pdf only).
    sent = [s for s in harness.messenger.sent
            if s["template_key"] == "onboarding.invoice.bulk.submitted"]
    assert sent, "expected onboarding.invoice.bulk.submitted to fire"
    failure_block = sent[-1]["variables"].get("failure_block") or ""
    assert "bad.pdf" in failure_block, (
        "the failed member must be surfaced to the SME; got: "
        + repr(failure_block)
    )


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
    """UAT 2026-06-18 (Ishan Bug 1) — SUBMIT-FIRST: each upload appends
    one record to ``state.invoices_submitted`` directly (no Approve
    handshake)."""
    await _drive_to_activated(harness)
    runtime = harness.platform.runtime

    # 1st invoice: one inbound, one submit, one ledger entry.
    await runtime.resume(
        WA, IDENTITY,
        message={"attachments": [{"filename": "INV-1.pdf", "content_base64": DOC}]},
    )

    # 2nd invoice: same.
    await runtime.resume(
        WA, IDENTITY,
        message={"attachments": [{"filename": "INV-2.pdf", "content_base64": DOC}]},
    )

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
    # Records carry the minimal submit-first contract: id, filename,
    # status. OCR-enriched fields land later via the webhook path.
    record = invoices[0]
    for field in ("invoice_id", "filename", "status"):
        assert field in record


async def test_invoice_upload_fires_processing_ack_before_submit(harness) -> None:
    """UAT 2026-06-18 (Ishan Bug 1) — SUBMIT-FIRST: the "Got your
    invoice — reading it now" ack fires BEFORE the backend submit
    round-trip (which now returns in <5s but still benefits from an
    immediate ack)."""
    await _drive_to_activated(harness)
    runtime = harness.platform.runtime

    await runtime.resume(
        WA, IDENTITY,
        message={"attachments": [{"filename": "INV-1.pdf", "content_base64": DOC}]},
    )

    templates = harness.messenger.templates()
    assert "onboarding.invoice.processing" in templates
    assert "onboarding.invoice.submitted" in templates
    # Processing ack precedes the submitted ack.
    assert templates.index("onboarding.invoice.processing") < templates.index(
        "onboarding.invoice.submitted"
    )


async def test_invoice_submit_failure_uses_retry_message(harness) -> None:
    """UAT 2026-06-18 (Ishan Bug 1): on a transient backend hiccup
    (extract_and_submit raises), the SME gets a polite retry message —
    NOT the old "we couldn't read the file" blame. Per Ishan: only the
    "no file bytes" path still asks the SME to resend."""
    await _drive_to_activated(harness)

    failing = InMemoryInvoiceClient(extract_error=RuntimeError("backend hiccup"))
    harness.platform.workflow._invoices = failing  # type: ignore[union-attr]
    harness.invoices = failing

    runtime = harness.platform.runtime
    await runtime.resume(
        WA, IDENTITY,
        message={"attachments": [{"filename": "INV-bad.pdf", "content_base64": DOC}]},
    )

    sent_failures = [
        s for s in harness.messenger.sent
        if s["template_key"] == "onboarding.invoice.failed"
    ]
    assert sent_failures, "expected the polite retry message on submit failure"
    reason = sent_failures[-1]["variables"]["reason"]
    # New message blames the backend, not the SME's file.
    assert "couldn't read the file" not in reason
    assert "try once more" in reason or "issue submitting" in reason
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
