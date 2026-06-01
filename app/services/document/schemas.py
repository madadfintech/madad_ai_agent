"""DTOs for the Document Intelligence API."""

from __future__ import annotations

from pydantic import BaseModel, Field

from .enums import DocumentKind, DocumentSource, DocumentStatus
from .models import DocumentBatch, DocumentRecord


class DocumentDTO(BaseModel):
    document_id: str
    application_ref: str | None
    batch_id: str | None
    kind: DocumentKind
    source: DocumentSource
    filename: str
    status: DocumentStatus
    document_type: str | None
    valid: bool | None
    validation_errors: list[str]
    madad_ref: str | None  # reference to the document stored in Madad's GCP
    attempts: int
    last_error: str | None

    @classmethod
    def from_model(cls, document: DocumentRecord) -> DocumentDTO:
        return cls(
            document_id=document.document_id,
            application_ref=document.application_ref,
            batch_id=document.batch_id,
            kind=document.kind,
            source=document.source,
            filename=document.filename,
            status=document.status,
            document_type=document.document_type,
            valid=document.valid,
            validation_errors=document.validation_errors,
            madad_ref=document.madad_ref,
            attempts=document.attempts,
            last_error=document.last_error,
        )


class BatchDTO(BaseModel):
    batch_id: str
    application_ref: str | None
    kind: DocumentKind
    checklist: str | None
    document_count: int
    documents: list[DocumentDTO] = Field(default_factory=list)

    @classmethod
    def from_model(cls, batch: DocumentBatch, documents: list[DocumentRecord]) -> BatchDTO:
        return cls(
            batch_id=batch.batch_id,
            application_ref=batch.application_ref,
            kind=batch.kind,
            checklist=batch.checklist,
            document_count=batch.document_count,
            documents=[DocumentDTO.from_model(d) for d in documents],
        )
