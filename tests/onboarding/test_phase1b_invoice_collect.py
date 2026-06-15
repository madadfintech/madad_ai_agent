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


async def test_single_invoice_routes_through_extract_and_submit(harness) -> None:
    await _drive_to_activated(harness)
    runtime = harness.platform.runtime

    result = await runtime.resume(
        WA, IDENTITY,
        message={"attachments": [{"filename": "INV-1.pdf", "content_base64": DOC}]},
    )

    # Run stays parked at invoice_collect for the next invoice.
    assert result.status == RunStatus.WAITING_FOR_INPUT
    assert result.prompt == {"waiting_for": "invoice", "step": "invoice_collect"}

    # The new client got called via the preferred extract+submit path
    # (not the deprecated KYC upload_invoice tool).
    inv_calls = [name for name, _ in harness.invoices.calls]
    assert inv_calls == ["extract_and_submit_base64"]
    assert "submit_zip_base64" not in inv_calls

    # The receipt template was used (not the catch-all help.contextual).
    assert "onboarding.invoice.received" in harness.messenger.templates()


async def test_zip_invoice_routes_through_submit_zip(harness) -> None:
    await _drive_to_activated(harness)
    runtime = harness.platform.runtime

    # Build a real ZIP archive so `_is_zip_attachment` recognises it.
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
    assert "submit_zip_base64" in inv_calls
    assert "extract_and_submit_base64" not in inv_calls


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
    await _drive_to_activated(harness)
    runtime = harness.platform.runtime

    await runtime.resume(
        WA, IDENTITY,
        message={"attachments": [{"filename": "INV-1.pdf", "content_base64": DOC}]},
    )
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
