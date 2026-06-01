"""MADAD Document Intelligence Service.

Document ingestion + processing ORCHESTRATION only. It does NOT run OCR, use any
external OCR, or store documents/extracted data. Each document is routed to
Madad's existing extraction microservice (via MCP), which stores it in Madad's
GCP buckets, extracts/classifies/validates it, and returns a classification, a
validity verdict, and a Madad reference. We retain only orchestration metadata
and drive the (CMS-defined) dynamic checklist. ZIP unpacking and review-CSV
generation are transient (in-memory) and never persisted.
"""

from __future__ import annotations

from app.shared.mcp import MCPToolCaller  # re-exported for backward compat

from .audit import DocumentAuditEntry, DocumentAuditLogger
from .checklist import (
    ChecklistProvider,
    ChecklistStatus,
    CmsChecklistProvider,
    InMemoryChecklistProvider,
    RequiredDocument,
)
from .deps import build_document_service, get_document_service
from .enums import DocumentKind, DocumentSource, DocumentStatus
from .errors import (
    BatchNotFoundError,
    DocumentError,
    DocumentNotFoundError,
    MadadDocumentError,
    ZipExtractionError,
)
from .events import (
    DocumentEvent,
    DocumentEventBus,
    DocumentEventType,
    InMemoryDocumentEventBus,
)
from .gateways import (
    InMemoryMadadDocumentGateway,
    MadadDocumentGateway,
    MadadDocumentResult,
    McpMadadDocumentGateway,
)
from .models import DocumentBatch, DocumentRecord
from .persistence import (
    BatchStore,
    DocumentStore,
    InMemoryBatchStore,
    InMemoryDocumentStore,
)
from .processing import extract_zip, generate_invoice_csv
from .service import DocumentConfig, DocumentIntelligenceService

__all__ = [
    # service
    "DocumentIntelligenceService",
    "DocumentConfig",
    "build_document_service",
    "get_document_service",
    # enums
    "DocumentKind",
    "DocumentSource",
    "DocumentStatus",
    # models
    "DocumentRecord",
    "DocumentBatch",
    # gateway (Madad extraction microservice via MCP)
    "MadadDocumentGateway",
    "InMemoryMadadDocumentGateway",
    "McpMadadDocumentGateway",
    "MadadDocumentResult",
    "MCPToolCaller",
    # checklist
    "ChecklistProvider",
    "InMemoryChecklistProvider",
    "CmsChecklistProvider",
    "RequiredDocument",
    "ChecklistStatus",
    # processing (transient)
    "extract_zip",
    "generate_invoice_csv",
    # persistence
    "DocumentStore",
    "BatchStore",
    "InMemoryDocumentStore",
    "InMemoryBatchStore",
    # events
    "DocumentEvent",
    "DocumentEventType",
    "DocumentEventBus",
    "InMemoryDocumentEventBus",
    # audit
    "DocumentAuditLogger",
    "DocumentAuditEntry",
    # errors
    "DocumentError",
    "DocumentNotFoundError",
    "BatchNotFoundError",
    "MadadDocumentError",
    "ZipExtractionError",
]
