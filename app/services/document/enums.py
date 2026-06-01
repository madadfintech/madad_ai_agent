"""Enumerations for the Document Intelligence service."""

from __future__ import annotations

from enum import StrEnum


class DocumentSource(StrEnum):
    WHATSAPP = "whatsapp"
    EMAIL = "email"
    ZIP = "zip"  # extracted from an uploaded archive
    UPLOAD = "upload"  # direct file upload
    API = "api"


class DocumentKind(StrEnum):
    """What collection a document belongs to."""

    ONBOARDING = "onboarding"
    INVOICE = "invoice"


class DocumentStatus(StrEnum):
    """Processing lifecycle of a single document.

    RECEIVED -> (routed to Madad's extraction microservice, which stores + extracts
    + validates) -> COMPLETED (valid, stored at Madad) | REJECTED (failed
    validation) | FAILED (transient routing failure exhausted retries).

    NOTE: the agentic platform never stores the document or its extracted data —
    only this orchestration status and Madad's reference.
    """

    RECEIVED = "received"
    COMPLETED = "completed"
    REJECTED = "rejected"
    FAILED = "failed"


TERMINAL_STATUSES: frozenset[DocumentStatus] = frozenset(
    {DocumentStatus.COMPLETED, DocumentStatus.REJECTED, DocumentStatus.FAILED}
)
