"""Document Intelligence domain models.

Orchestration metadata ONLY. By design (data sovereignty) we keep no document
bytes and no extracted fields — those live in Madad's GCP buckets and core DB.
We retain the classification, a validity verdict, and Madad's reference so the
agent can drive the conversation and the checklist.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field

from app.shared.workflow.utils import new_id, utcnow

from .enums import DocumentKind, DocumentSource, DocumentStatus


class DocumentBatch(BaseModel):
    """A group of documents submitted together (e.g. a ZIP, or one application)."""

    batch_id: str = Field(default_factory=lambda: new_id("dbatch"))
    application_ref: str | None = None
    kind: DocumentKind = DocumentKind.ONBOARDING
    checklist: str | None = None
    source: DocumentSource = DocumentSource.UPLOAD
    document_count: int = 0
    correlation_id: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime = Field(default_factory=utcnow)
    updated_at: datetime = Field(default_factory=utcnow)


class DocumentRecord(BaseModel):
    """Orchestration record for a single document — no content, no extracted data."""

    document_id: str = Field(default_factory=lambda: new_id("doc"))
    application_ref: str | None = None
    batch_id: str | None = None
    kind: DocumentKind = DocumentKind.ONBOARDING
    source: DocumentSource = DocumentSource.UPLOAD

    filename: str  # metadata only
    provider_ref: str | None = None  # channel media id (a reference, not content)

    status: DocumentStatus = DocumentStatus.RECEIVED
    document_type: str | None = None  # classification (from Madad)
    valid: bool | None = None
    validation_errors: list[str] = Field(default_factory=list)
    madad_ref: str | None = None  # reference to the document stored in Madad's GCP

    attempts: int = 0
    last_error: str | None = None

    correlation_id: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime = Field(default_factory=utcnow)
    updated_at: datetime = Field(default_factory=utcnow)
