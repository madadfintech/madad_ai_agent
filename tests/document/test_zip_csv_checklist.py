"""ZIP extraction (transient), CSV generation (transient), and checklist tracking."""

from __future__ import annotations

import pytest

from app.services.document import (
    DocumentEventType,
    DocumentIntelligenceService,
    DocumentStatus,
    ZipExtractionError,
    extract_zip,
    generate_invoice_csv,
)

from .conftest import make_zip

APP = "APP-200"


async def test_zip_extraction_routes_each_entry(make_harness):
    harness = make_harness(types={"trade": "trade_license", "tax": "tax_card"})
    content = make_zip(
        {"Trade_License.pdf": b"a", "Tax_Card.pdf": b"b", "__MACOSX/._junk": b"x"}
    )

    batch = await harness.service.ingest_zip(
        "docs.zip", content, application_ref=APP, checklist="onboarding"
    )

    assert batch.document_count == 2  # cruft entry ignored
    documents = await harness.service.list_batch_documents(batch.batch_id)
    assert {d.document_type for d in documents} == {"trade_license", "tax_card"}
    assert all(d.status == DocumentStatus.COMPLETED for d in documents)
    assert DocumentEventType.ZIP_EXTRACTED in harness.event_types()


def test_extract_zip_rejects_non_zip():
    with pytest.raises(ZipExtractionError):
        extract_zip(b"this is not a zip")


def test_generate_invoice_csv_columns_and_rows():
    csv_bytes = generate_invoice_csv(
        [
            {"invoice_no": "INV-1", "amount": "32000", "customer": "Acme", "due_date": "28 May"},
            {"invoice_no": "INV-2", "amount": "24000", "customer": "Roads", "due_date": "22 Jun"},
        ]
    )
    lines = csv_bytes.decode("utf-8").strip().splitlines()
    assert lines[0].startswith("Row,Invoice No.")
    assert lines[1].startswith("1,") and "INV-1" in lines[1]
    assert lines[2].startswith("2,") and "INV-2" in lines[2]


def test_build_invoice_csv_is_transient():
    # Generated from rows supplied by Madad; returned to caller, never stored.
    csv_bytes = DocumentIntelligenceService.build_invoice_csv([{"invoice_no": "INV-9"}])
    assert b"INV-9" in csv_bytes


async def test_checklist_detects_missing_documents(make_harness):
    harness = make_harness(types={"trade": "trade_license", "tax": "tax_card"})
    harness.checklist.add("onboarding", ["trade_license", "tax_card", "audited_report"])

    await harness.service.ingest_document("Trade_License.pdf", content=b"a", application_ref=APP)
    status = await harness.service.checklist_status("onboarding", application_ref=APP)
    assert status.validated == ["trade_license"]
    assert status.missing == ["tax_card", "audited_report"]
    assert status.complete is False

    await harness.service.ingest_document("Tax_Card.pdf", content=b"b", application_ref=APP)
    status = await harness.service.checklist_status("onboarding", application_ref=APP)
    assert "tax_card" not in status.missing
    assert "audited_report" in status.missing
