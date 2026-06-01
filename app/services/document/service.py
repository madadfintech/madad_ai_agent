"""Document Intelligence orchestration (data-sovereign).

The agentic platform never stores documents or extracted data. Each document is
routed to Madad's extraction microservice (via MCP), which stores it in Madad's
GCP buckets, extracts/classifies/validates it, and returns a classification, a
validity verdict, and a Madad reference. We persist only orchestration metadata
and drive the (CMS-defined) checklist.

ZIP archives are unpacked in memory (transiently) and each entry routed to Madad;
the bulk-invoice review CSV is generated transiently and returned to the caller.
Neither the archive, the entries, nor the CSV are persisted by us.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from app.core.logging import get_logger
from app.shared.workflow.context import Clock, SystemClock
from app.shared.workflow.utils import compute_backoff

from .audit import DocumentAuditLogger
from .checklist import ChecklistProvider, ChecklistStatus
from .enums import DocumentKind, DocumentSource, DocumentStatus
from .errors import BatchNotFoundError, DocumentNotFoundError, MadadDocumentError
from .events import DocumentEvent, DocumentEventBus, DocumentEventType
from .gateways import MadadDocumentGateway
from .models import DocumentBatch, DocumentRecord
from .persistence import BatchStore, DocumentStore
from .processing import extract_zip, generate_invoice_csv

SleepFn = Callable[[float], Awaitable[None]]


@dataclass
class DocumentConfig:
    max_attempts: int = 3
    retry_base_delay: float = 1.0
    retry_max_delay: float = 30.0
    retry_jitter: bool = True


class DocumentIntelligenceService:
    """Routes documents to Madad, tracks orchestration status, drives checklists."""

    def __init__(
        self,
        *,
        documents: DocumentStore,
        batches: BatchStore,
        gateway: MadadDocumentGateway,
        checklist_provider: ChecklistProvider,
        events: DocumentEventBus,
        audit: DocumentAuditLogger,
        config: DocumentConfig | None = None,
        sleep: SleepFn | None = None,
        clock: Clock | None = None,
        logger: Any | None = None,
    ) -> None:
        self._documents = documents
        self._batches = batches
        self._gateway = gateway
        self._checklists = checklist_provider
        self._events = events
        self._audit = audit
        self._config = config or DocumentConfig()
        self._sleep: SleepFn = sleep or asyncio.sleep
        self._clock = clock or SystemClock()
        self._log = logger or get_logger("document.service")

    @property
    def events(self) -> DocumentEventBus:
        """The in-process event bus (for forwarding onto the unified bus)."""

        return self._events

    # -- ingestion ------------------------------------------------------------

    async def ingest_document(
        self,
        filename: str,
        *,
        application_ref: str | None = None,
        kind: DocumentKind = DocumentKind.ONBOARDING,
        source: DocumentSource = DocumentSource.UPLOAD,
        provider_ref: str | None = None,
        content: bytes | None = None,
        batch_id: str | None = None,
        correlation_id: str | None = None,
    ) -> DocumentRecord:
        """Route a single document to Madad's extraction microservice.

        ``content`` (when present) is forwarded transiently and never stored; in
        production, channel documents arrive as a ``provider_ref`` that Madad
        fetches.
        """

        document = DocumentRecord(
            application_ref=application_ref,
            batch_id=batch_id,
            kind=kind,
            source=source,
            filename=filename,
            provider_ref=provider_ref,
            correlation_id=correlation_id,
            status=DocumentStatus.RECEIVED,
        )
        await self._documents.create(document)
        await self._emit(DocumentEventType.DOCUMENT_RECEIVED, document)
        await self._audit.record(
            "received", document_id=document.document_id, detail={"filename": filename}
        )

        await self._route_to_madad(document, content=content)
        return await self._require_document(document.document_id)

    async def ingest_zip(
        self,
        filename: str,
        content: bytes,
        *,
        application_ref: str | None = None,
        kind: DocumentKind = DocumentKind.ONBOARDING,
        source: DocumentSource = DocumentSource.WHATSAPP,
        checklist: str | None = None,
        correlation_id: str | None = None,
    ) -> DocumentBatch:
        """Unpack a ZIP in memory and route each entry to Madad (nothing stored)."""

        batch = DocumentBatch(
            application_ref=application_ref,
            kind=kind,
            checklist=checklist,
            source=source,
            correlation_id=correlation_id,
        )
        await self._batches.create(batch)

        entries = extract_zip(content)
        await self._emit(
            DocumentEventType.ZIP_EXTRACTED, None, batch_id=batch.batch_id,
            payload={"count": len(entries), "filename": filename},
        )
        await self._audit.record(
            "zip_extracted", batch_id=batch.batch_id, detail={"count": len(entries)}
        )

        for entry_name, entry_bytes in entries:
            await self.ingest_document(
                entry_name,
                application_ref=application_ref,
                kind=kind,
                source=DocumentSource.ZIP,
                content=entry_bytes,
                batch_id=batch.batch_id,
                correlation_id=correlation_id,
            )

        batch.document_count = len(entries)
        await self._batches.save(batch)
        return await self._require_batch(batch.batch_id)

    # -- routing to Madad (with inline retry) --------------------------------

    async def _route_to_madad(
        self, document: DocumentRecord, *, content: bytes | None
    ) -> None:
        attempts = max(1, self._config.max_attempts)
        for attempt in range(attempts):
            document.attempts += 1
            try:
                result = await self._gateway.process_document(
                    application_ref=document.application_ref,
                    filename=document.filename,
                    kind=document.kind,
                    provider_ref=document.provider_ref,
                    content=content,
                )
            except MadadDocumentError as exc:
                document.last_error = f"{type(exc).__name__}: {exc}"
                if attempt < attempts - 1:
                    delay = compute_backoff(
                        document.attempts,
                        base_delay=self._config.retry_base_delay,
                        max_delay=self._config.retry_max_delay,
                        jitter=self._config.retry_jitter,
                    )
                    await self._documents.save(document)
                    await self._emit(
                        DocumentEventType.DOCUMENT_RETRYING, document,
                        payload={"attempt": document.attempts},
                    )
                    await self._sleep(delay)
                    continue
                document.status = DocumentStatus.FAILED
                await self._documents.save(document)
                await self._emit(DocumentEventType.DOCUMENT_FAILED, document)
                await self._audit.record(
                    "failed", document_id=document.document_id,
                    detail={"error": document.last_error},
                )
                return

            # Madad processed it (stored + extracted + validated).
            document.document_type = result.document_type
            document.valid = result.valid
            document.validation_errors = result.validation_errors
            document.madad_ref = result.madad_ref
            document.last_error = None
            if result.valid:
                document.status = DocumentStatus.COMPLETED
                await self._documents.save(document)
                await self._emit(
                    DocumentEventType.DOCUMENT_COMPLETED, document,
                    payload={"document_type": result.document_type, "madad_ref": result.madad_ref},
                )
                await self._audit.record(
                    "completed", document_id=document.document_id,
                    detail={"document_type": result.document_type},
                )
            else:
                document.status = DocumentStatus.REJECTED
                await self._documents.save(document)
                await self._emit(
                    DocumentEventType.DOCUMENT_REJECTED, document,
                    payload={"errors": result.validation_errors},
                )
                await self._audit.record(
                    "rejected", document_id=document.document_id,
                    detail={"errors": result.validation_errors},
                )
            await self._refresh_checklist(document)
            return

    # -- CSV (transient, never persisted) ------------------------------------

    @staticmethod
    def build_invoice_csv(rows: list[dict[str, Any]]) -> bytes:
        """Build the bulk-invoice review CSV from rows supplied by Madad.

        Returned to the caller (to send to the SME) — never stored by us.
        """

        return generate_invoice_csv(rows)

    # -- checklist ------------------------------------------------------------

    async def checklist_status(
        self,
        checklist: str,
        *,
        application_ref: str | None = None,
        batch_id: str | None = None,
    ) -> ChecklistStatus:
        required = await self._checklists.get_required(checklist)
        if batch_id is not None:
            documents = await self._documents.list_by_batch(batch_id)
        elif application_ref is not None:
            documents = await self._documents.list_by_application(application_ref)
        else:
            documents = []

        validated = {
            d.document_type
            for d in documents
            if d.document_type and d.status == DocumentStatus.COMPLETED
        }
        received = {d.document_type for d in documents if d.document_type}
        rejected = {
            d.document_type
            for d in documents
            if d.document_type and d.status == DocumentStatus.REJECTED
        }
        missing = [r.code for r in required if r.required and r.code not in validated]
        return ChecklistStatus(
            checklist=checklist,
            received=sorted(t for t in received if t),
            validated=sorted(t for t in validated if t),
            rejected=sorted(t for t in rejected if t),
            missing=missing,
        )

    # -- reads ----------------------------------------------------------------

    async def get_document(self, document_id: str) -> DocumentRecord:
        return await self._require_document(document_id)

    async def get_batch(self, batch_id: str) -> DocumentBatch:
        return await self._require_batch(batch_id)

    async def list_batch_documents(self, batch_id: str) -> list[DocumentRecord]:
        return await self._documents.list_by_batch(batch_id)

    # -- helpers --------------------------------------------------------------

    async def _refresh_checklist(self, document: DocumentRecord) -> None:
        batch = await self._batches.get(document.batch_id) if document.batch_id else None
        checklist = batch.checklist if batch else None
        if checklist is None:
            return
        status = await self.checklist_status(
            checklist, application_ref=document.application_ref, batch_id=document.batch_id
        )
        await self._emit(
            DocumentEventType.CHECKLIST_UPDATED, document, batch_id=document.batch_id,
            payload={"missing": status.missing, "complete": status.complete},
        )

    async def _require_document(self, document_id: str) -> DocumentRecord:
        document = await self._documents.get(document_id)
        if document is None:
            raise DocumentNotFoundError(
                f"Document {document_id!r} not found", details={"document_id": document_id}
            )
        return document

    async def _require_batch(self, batch_id: str) -> DocumentBatch:
        batch = await self._batches.get(batch_id)
        if batch is None:
            raise BatchNotFoundError(
                f"Batch {batch_id!r} not found", details={"batch_id": batch_id}
            )
        return batch

    async def _emit(
        self,
        event_type: DocumentEventType,
        document: DocumentRecord | None,
        *,
        batch_id: str | None = None,
        payload: dict[str, Any] | None = None,
    ) -> None:
        await self._events.publish(
            DocumentEvent(
                type=event_type,
                document_id=document.document_id if document else None,
                batch_id=batch_id or (document.batch_id if document else None),
                application_ref=document.application_ref if document else None,
                payload=payload or {},
            )
        )
