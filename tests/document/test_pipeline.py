"""Document routing pipeline: route to Madad, rejection, retry, sovereignty."""

from __future__ import annotations

from app.services.document import DocumentEventType, DocumentStatus

APP = "APP-100"


async def test_routes_to_madad_and_completes(make_harness):
    harness = make_harness(types={"trade": "trade_license"})
    document = await harness.service.ingest_document(
        "Trade_License.pdf", content=b"%PDF-bytes", application_ref=APP
    )

    assert document.status == DocumentStatus.COMPLETED
    assert document.document_type == "trade_license"
    assert document.madad_ref is not None  # stored in Madad's GCP, we keep the ref
    assert harness.gateway.processed == ["Trade_License.pdf"]

    types = harness.event_types()
    assert DocumentEventType.DOCUMENT_RECEIVED in types
    assert DocumentEventType.DOCUMENT_COMPLETED in types


async def test_invalid_document_rejected(harness):
    document = await harness.service.ingest_document(
        "invalid_taxcard.pdf", content=b"data", application_ref=APP
    )
    assert document.status == DocumentStatus.REJECTED
    assert document.valid is False
    assert document.madad_ref is None
    assert DocumentEventType.DOCUMENT_REJECTED in harness.event_types()


async def test_transient_failure_retries_then_completes(make_harness):
    harness = make_harness(gateway_fail=2, max_attempts=3)
    document = await harness.service.ingest_document("doc.pdf", content=b"x", application_ref=APP)

    assert document.status == DocumentStatus.COMPLETED
    assert document.attempts == 3
    assert DocumentEventType.DOCUMENT_RETRYING in harness.event_types()


async def test_retry_exhaustion_fails(make_harness):
    harness = make_harness(gateway_fail=100, max_attempts=2)
    document = await harness.service.ingest_document("doc.pdf", content=b"x", application_ref=APP)

    assert document.status == DocumentStatus.FAILED
    assert document.last_error is not None
    assert DocumentEventType.DOCUMENT_FAILED in harness.event_types()


async def test_no_document_content_or_extracted_data_is_persisted(make_harness):
    """Data sovereignty: the record holds no bytes and no extracted fields — only
    a Madad reference + classification."""

    harness = make_harness(types={"trade": "trade_license"})
    document = await harness.service.ingest_document(
        "Trade_License.pdf", content=b"super-secret-document-bytes", application_ref=APP
    )

    dumped = document.model_dump()
    assert "fields" not in dumped  # no extracted SME data
    assert "storage_ref" not in dumped  # no local staging
    assert b"super-secret" not in repr(dumped).encode()  # no document bytes retained
    assert document.madad_ref is not None  # only a reference to Madad's copy
