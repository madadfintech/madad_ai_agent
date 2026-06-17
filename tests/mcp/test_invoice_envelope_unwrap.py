"""UAT 2026-06-18 (Ishan diagnosis on +919497191690): the extract-only
path returns a deeper envelope than submit, and uses sme_name / buyer_name
instead of supplier_name / customer_name. The agent's old envelope unwrap
only handled ``{invoice}`` / ``{body}`` / flat dict, so every real extract
came back as ``{data, filename, message, success}`` → ``_draft_is_empty``
returned True → the SME got "We couldn't read the key details" on every
upload, even when OCR succeeded. These tests pin the new unwrap shape."""

from __future__ import annotations

from app.services.workflow.mcp_invoices import _normalise_invoice_envelope


def test_extract_envelope_with_fields_flattens_to_canonical_draft() -> None:
    """The real extract-only response shape — flatten fields up and
    rename sme_name / buyer_name to supplier_name / customer_name."""
    response = {
        "success": True,
        "message": "ok",
        "data": {
            "gcsUri": "gs://bucket/8044-invoice.pdf",
            "signedUrl": "https://...",
            "extractionOnly": True,
            "extractionSuccess": True,
            "extractedData": {
                "document_type": "Invoice",
                "fields": {
                    "sme_name": "Global Oils Factory",
                    "buyer_name": "BIG TRADERS",
                    "total_amount": "24600",
                    "currency": "QAR",
                    "invoice_number": "INV-8044",
                    "invoice_date": "2025-02-24",
                    "due_date": "2025-03-24",
                },
            },
        },
    }

    draft = _normalise_invoice_envelope(response, "8044-invoice.pdf")

    assert draft["supplier_name"] == "Global Oils Factory"
    assert draft["customer_name"] == "BIG TRADERS"
    assert draft["total_amount"] == "24600"
    assert draft["currency"] == "QAR"
    assert draft["invoice_number"] == "INV-8044"
    assert draft["invoice_date"] == "2025-02-24"
    assert draft["due_date"] == "2025-03-24"
    assert draft["document_type"] == "Invoice"
    assert draft["filename"] == "8044-invoice.pdf"


def test_extract_envelope_with_partial_fields_drops_empty_values() -> None:
    """Real-world: extractor leaves some fields blank. Only non-empty
    values land in the draft so ``_draft_is_empty`` correctly reports
    what's populated."""
    response = {
        "success": True,
        "data": {
            "extractedData": {
                "fields": {
                    "sme_name": "ACME Co",
                    "buyer_name": "",  # not extracted
                    "total_amount": "5000",
                    "currency": "QAR",
                    "invoice_number": None,
                    "invoice_date": "2025-01-01",
                    "due_date": "",
                },
            },
        },
    }

    draft = _normalise_invoice_envelope(response, "x.pdf")

    assert draft["supplier_name"] == "ACME Co"
    assert "customer_name" not in draft
    assert draft["total_amount"] == "5000"
    assert "invoice_number" not in draft
    assert "due_date" not in draft


def test_submit_envelope_with_invoice_key_still_works() -> None:
    """Legacy path: submit-style response ``{invoice: {...}}`` keeps the
    existing unwrap. No regression for the submit_base64 flow."""
    response = {
        "invoice": {
            "id": "inv-123",
            "supplier_name": "ACME Co",
            "total_amount": "5000",
        },
    }

    draft = _normalise_invoice_envelope(response, "x.pdf")

    assert draft["invoice_id"] == "inv-123"
    assert draft["supplier_name"] == "ACME Co"
    assert draft["total_amount"] == "5000"
    assert draft["filename"] == "x.pdf"


def test_flat_dict_response_still_works() -> None:
    """A bare invoice dict (no envelope) is treated as-is."""
    response = {"supplier_name": "ACME Co", "total_amount": "5000"}

    draft = _normalise_invoice_envelope(response, "x.pdf")

    assert draft["supplier_name"] == "ACME Co"
    assert draft["total_amount"] == "5000"
    assert draft["filename"] == "x.pdf"


def test_non_dict_response_is_safe() -> None:
    """Defensive: a non-dict response yields a draft with raw payload."""
    draft = _normalise_invoice_envelope("unexpected string", "x.pdf")

    assert draft["filename"] == "x.pdf"
    assert draft["raw"] == "unexpected string"
