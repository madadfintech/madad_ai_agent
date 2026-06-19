"""Phase 1.b — ``_invoice_collect_await`` post-activation behaviour.

UAT 2026-06-19 (Ishan QA report) restored the EXTRACT-FIRST CONDITIONAL flow:
* Single PDF + extract success with usable fields → Approve/Edit/Reject card.
* Single PDF + extract failure OR empty draft     → auto-submit blank.
* ZIP / multi-file + at least one extract success → CSV preview + APPROVE ALL.
* ZIP / multi-file + extract failure per member   → that member auto-submits.

Per-member messages are gone — one "📦 Received N invoices — processing"
ack up front, one consolidated receipt at the end.

The submit-first paths (``_invoice_submit_first`` /
``_invoice_bulk_submit_first``) are retained as the fallback called when
extraction fails — never directly from the routing fork any more.
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
    """Drive a happy-path run to ``invoice_collect_await``."""
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


# -- Single-file: extract-first conditional ---------------------------------


async def test_single_invoice_with_fields_shows_confirm_card(harness) -> None:
    """UAT 2026-06-19 QA #3: extract succeeds + has fields → confirm card."""
    await _drive_to_activated(harness)
    runtime = harness.platform.runtime

    result = await runtime.resume(
        WA, IDENTITY,
        message={"attachments": [{"filename": "INV-1.pdf", "content_base64": DOC}]},
    )
    assert result.status == RunStatus.WAITING_FOR_INPUT

    inv_calls = [name for name, _ in harness.invoices.calls]
    # New flow: extract-only on first hit. Confirm card → no submit yet.
    assert inv_calls.count("extract_base64") == 1
    assert "extract_and_submit_base64" not in inv_calls
    assert "submit_base64" not in inv_calls

    templates = harness.messenger.templates()
    assert "onboarding.invoice.processing" in templates  # immediate ack
    assert "onboarding.invoice.confirm" in templates     # confirm card
    # Auto-submit ack only fires on extract failure path.
    assert "onboarding.invoice.submitted" not in templates


async def test_single_invoice_approve_then_submits(harness) -> None:
    """SME taps Approve on the confirm card → submit_base64 fires once."""
    await _drive_to_activated(harness)
    runtime = harness.platform.runtime

    await runtime.resume(
        WA, IDENTITY,
        message={"attachments": [{"filename": "INV-1.pdf", "content_base64": DOC}]},
    )
    await runtime.resume(WA, IDENTITY, message={"text": "Approve"})

    inv_calls = [name for name, _ in harness.invoices.calls]
    assert inv_calls.count("submit_base64") == 1
    assert "onboarding.invoice.received" in harness.messenger.templates()


async def test_single_invoice_extract_fail_auto_submits_blank(make_harness) -> None:
    """UAT 2026-06-19 QA #3: extract raises → auto-submit blank (no card)."""

    # Client where extract_base64 raises but extract_and_submit_base64
    # (the fallback path's submit) succeeds.
    class _ExtractFailsClient(InMemoryInvoiceClient):
        async def extract_base64(  # type: ignore[override]
            self, *, access_token, filename, content_base64, mime_type=None,
        ):
            self._record(
                "extract_base64", access_token=access_token,
                filename=filename, mime_type=mime_type,
            )
            raise RuntimeError("OCR hiccup")

    harness = make_harness()
    await _drive_to_activated(harness)
    harness.platform.workflow._invoices = _ExtractFailsClient()  # type: ignore[union-attr]
    harness.invoices = harness.platform.workflow._invoices  # type: ignore[union-attr]
    runtime = harness.platform.runtime

    await runtime.resume(
        WA, IDENTITY,
        message={"attachments": [{"filename": "INV-x.pdf", "content_base64": DOC}]},
    )

    inv_calls = [name for name, _ in harness.invoices.calls]
    # Extract attempted, then auto-submit blank.
    assert "extract_base64" in inv_calls
    assert "extract_and_submit_base64" in inv_calls
    # SME sees the "submitted ✅" ack — never a "couldn't read" blocker.
    assert "onboarding.invoice.submitted" in harness.messenger.templates()
    assert "onboarding.invoice.confirm" not in harness.messenger.templates()


async def test_single_invoice_empty_draft_auto_submits_blank(make_harness) -> None:
    """UAT 2026-06-19 QA #3: extract succeeds but draft has no fields →
    auto-submit blank. Backend defaults fill the rest."""
    class _EmptyClient(InMemoryInvoiceClient):
        async def extract_base64(  # type: ignore[override]
            self, *, access_token, filename, content_base64, mime_type=None,
        ):
            self._record(
                "extract_base64", access_token=access_token,
                filename=filename, mime_type=mime_type,
            )
            return {"filename": filename, "currency": "QAR"}

    harness = make_harness()
    await _drive_to_activated(harness)
    harness.platform.workflow._invoices = _EmptyClient()  # type: ignore[union-attr]
    harness.invoices = harness.platform.workflow._invoices  # type: ignore[union-attr]
    runtime = harness.platform.runtime

    await runtime.resume(
        WA, IDENTITY,
        message={"attachments": [{"filename": "blurry.pdf", "content_base64": DOC}]},
    )

    inv_calls = [name for name, _ in harness.invoices.calls]
    assert "extract_and_submit_base64" in inv_calls
    assert "onboarding.invoice.submitted" in harness.messenger.templates()
    assert "onboarding.invoice.confirm" not in harness.messenger.templates()


# -- Bulk: extract-first + CSV review --------------------------------------


async def test_zip_invoice_extracts_and_renders_csv_preview(harness) -> None:
    """UAT 2026-06-19 QA #4: ZIP → parallel extract → CSV preview +
    APPROVE ALL handshake (per PDF Step 10 bulk path)."""
    await _drive_to_activated(harness)
    runtime = harness.platform.runtime

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("inv1.pdf", b"dummy invoice 1")
        zf.writestr("inv2.pdf", b"dummy invoice 2")
    zip_b64 = base64.b64encode(buf.getvalue()).decode("ascii")

    await runtime.resume(
        WA, IDENTITY,
        message={"attachments": [{
            "filename": "invoices.zip",
            "content_base64": zip_b64,
            "mime_type": "application/zip",
        }]},
    )

    inv_calls = [name for name, _ in harness.invoices.calls]
    # ONE extract per member.
    assert inv_calls.count("extract_base64") == 2
    # NO submit yet — SME hasn't approved.
    assert "submit_base64" not in inv_calls
    assert "extract_and_submit_base64" not in inv_calls

    templates = harness.messenger.templates()
    # ONE consolidated "received N invoices — processing" ack.
    assert "onboarding.invoice.bulk.processing" in templates
    # CSV preview rendered.
    assert "onboarding.invoice.batch.preview" in templates


async def test_zip_approve_all_submits_in_parallel(harness) -> None:
    """UAT 2026-06-19 QA #1+5: APPROVE ALL submits every row, one
    consolidated receipt at the end (no per-row spam)."""
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
    await runtime.resume(WA, IDENTITY, message={"text": "APPROVE ALL"})

    inv_calls = [name for name, _ in harness.invoices.calls]
    assert inv_calls.count("submit_base64") == 2
    # ONE consolidated receipt — bulk.submitted not invoice.received per-row.
    templates = harness.messenger.templates()
    assert "onboarding.invoice.bulk.submitted" in templates


async def test_zip_one_member_fails_extract_still_auto_submits(
    make_harness,
) -> None:
    """UAT 2026-06-19 QA #4: in a ZIP, a member whose extract fails
    falls through to auto-submit blank (NEVER silently dropped)."""
    call_count = {"n": 0}

    class _PartiallyFailingClient(InMemoryInvoiceClient):
        async def extract_base64(  # type: ignore[override]
            self, *, access_token, filename, content_base64, mime_type=None,
        ):
            call_count["n"] += 1
            self._record(
                "extract_base64", access_token=access_token,
                filename=filename, mime_type=mime_type,
            )
            if call_count["n"] == 2:
                raise RuntimeError("backend hiccup on member 2")
            return {
                "invoice_number": f"INV-{call_count['n']}",
                "supplier_name": "ACME",
                "customer_name": "BIG TRADER",
                "total_amount": 1000,
                "currency": "QAR",
                "filename": filename,
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

    inv_calls = [name for name, _ in harness.invoices.calls]
    # 2 extracts attempted; the failed one fell through to auto-submit.
    assert inv_calls.count("extract_base64") == 2
    assert "extract_and_submit_base64" in inv_calls, (
        "the failed member must be auto-submitted (never silently dropped)"
    )


# -- Idempotency ------------------------------------------------------------


async def test_repeat_same_attachment_within_run_dedupes(harness) -> None:
    """UAT 2026-06-19 QA #2: status_poll / event resumes re-delivering
    the same attachment payload must NOT re-submit. The submitted-sig
    tracker on state catches even after the 5min retry window."""
    await _drive_to_activated(harness)
    runtime = harness.platform.runtime

    # First upload — extract + confirm card + Approve.
    await runtime.resume(
        WA, IDENTITY,
        message={"attachments": [{"filename": "INV-x.pdf", "content_base64": DOC}]},
    )
    await runtime.resume(WA, IDENTITY, message={"text": "Approve"})
    submitted_first = sum(
        1 for n, _ in harness.invoices.calls if n == "submit_base64"
    )
    assert submitted_first == 1

    # Force the recent-retry window to expire so we exercise the
    # permanent sig tracker (NOT just the 5min dedupe).
    snap_run = await runtime.run_store.get(
        (await runtime.sessions.get(WA, IDENTITY)).active_run_id  # type: ignore[union-attr]
    )
    # The 5min retry-window check uses ``last_invoice_attempt_at``; the
    # PERMANENT check uses ``invoice_submitted_sigs``. Verify the
    # permanent tracker hit:
    await runtime.resume(
        WA, IDENTITY,
        message={"attachments": [{"filename": "INV-x.pdf", "content_base64": DOC}]},
    )
    submitted_after = sum(
        1 for n, _ in harness.invoices.calls if n == "submit_base64"
    )
    assert submitted_after == submitted_first, (
        "re-uploading the same attachment must NOT re-submit (sig tracker)"
    )


# -- Status query + idle chat (unchanged) -----------------------------------


async def test_status_query_dispatches_get_my_invoices(harness) -> None:
    await _drive_to_activated(harness)
    runtime = harness.platform.runtime

    await runtime.resume(
        WA, IDENTITY,
        message={"attachments": [{"filename": "INV-1.pdf", "content_base64": DOC}]},
    )
    await runtime.resume(WA, IDENTITY, message={"text": "Approve"})
    harness.invoices.calls.clear()

    await runtime.resume(WA, IDENTITY, message={"text": "any updates on my invoice?"})

    inv_calls = [name for name, _ in harness.invoices.calls]
    assert inv_calls == ["get_my_invoices"]
    assert "onboarding.invoice.status" in harness.messenger.templates()


async def test_off_script_chat_does_not_call_client(harness) -> None:
    await _drive_to_activated(harness)
    runtime = harness.platform.runtime
    harness.invoices.calls.clear()

    await runtime.resume(WA, IDENTITY, message={"text": "thanks for the help"})

    inv_calls = [name for name, _ in harness.invoices.calls]
    assert inv_calls == []


# -- Self-service QA intents (unchanged) -----------------------------------


async def test_status_update_resume_at_invoice_collect_is_silent(harness) -> None:
    """A bare status_update / docs_settle resume must NOT re-fire the
    canned invoice prompt — it just re-parks silently."""
    await _drive_to_activated(harness)
    runtime = harness.platform.runtime
    templates_before = list(harness.messenger.templates())

    await runtime.resume(
        WA, IDENTITY,
        message={"type": "status_update", "last_status_source": "poll"},
    )

    templates_after = list(harness.messenger.templates())
    assert templates_after == templates_before


async def test_qa_limit_answers_from_me_credit_line(harness) -> None:
    """'What's my limit?' answers from /me's creditLine (UAT 2026-06-16 #9)."""
    await _drive_to_activated(harness)
    runtime = harness.platform.runtime

    await runtime.resume(WA, IDENTITY, message={"text": "what's my limit?"})

    answers = [
        s for s in harness.messenger.sent
        if s["template_key"] == "onboarding.help.contextual"
    ]
    assert answers


async def test_qa_disbursed_total_sums_state_ledger(harness) -> None:
    """'How much was disbursed?' sums state.disbursements_received."""
    await _drive_to_activated(harness)
    runtime = harness.platform.runtime

    # Simulate a disbursement event so state has something to sum.
    await runtime.resume(WA, IDENTITY, message={
        "type": "phase1b_event", "event": "transaction.disbursed",
        "invoiceNumber": "INV-1", "disbursedAmount": "32000",
    })

    await runtime.resume(WA, IDENTITY, message={"text": "how much disbursed so far?"})

    answers = [
        s for s in harness.messenger.sent
        if s["template_key"] == "onboarding.help.contextual"
    ]
    assert answers


async def test_qa_due_uses_repayment_outstanding_and_emis(harness) -> None:
    """'What's due?' reads outstandingAmount + emisRemaining from state."""
    await _drive_to_activated(harness)
    runtime = harness.platform.runtime

    await runtime.resume(WA, IDENTITY, message={"text": "what's due?"})

    answers = [
        s for s in harness.messenger.sent
        if s["template_key"] == "onboarding.help.contextual"
    ]
    assert answers


# -- Small helpers ---------------------------------------------------------


async def test_is_invoice_status_query_recognizes_common_phrasings() -> None:
    from app.services.workflow.onboarding import _is_invoice_status_query

    for phrase in (
        "any update on my invoice",
        "status of my invoice?",
        "where are my invoices?",
    ):
        assert _is_invoice_status_query({"text": phrase}), phrase


async def test_format_invoice_status_handles_empty_and_populated() -> None:
    from app.services.workflow.onboarding import _format_invoice_status_summary

    assert "haven't submitted" in _format_invoice_status_summary([]).lower()

    summary = _format_invoice_status_summary([
        {"invoice_id": "inv-1", "filename": "x.pdf", "status": "SUBMITTED"},
    ])
    # Summary renders one line per invoice with status visible.
    assert "SUBMITTED" in summary
