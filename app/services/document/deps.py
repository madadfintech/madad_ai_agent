"""Dependency wiring for the Document Intelligence service."""

from __future__ import annotations

from functools import lru_cache
from typing import Any

from app.shared.workflow.context import Clock

from .audit import DocumentAuditLogger
from .checklist import ChecklistProvider, InMemoryChecklistProvider
from .events import DocumentEventBus, InMemoryDocumentEventBus
from .gateways import InMemoryMadadDocumentGateway, MadadDocumentGateway
from .persistence import (
    BatchStore,
    DocumentStore,
    InMemoryBatchStore,
    InMemoryDocumentStore,
)
from .service import DocumentConfig, DocumentIntelligenceService, SleepFn


def build_document_service(
    *,
    documents: DocumentStore | None = None,
    batches: BatchStore | None = None,
    gateway: MadadDocumentGateway | None = None,
    checklist_provider: ChecklistProvider | None = None,
    events: DocumentEventBus | None = None,
    config: DocumentConfig | None = None,
    sleep: SleepFn | None = None,
    clock: Clock | None = None,
) -> DocumentIntelligenceService:
    return DocumentIntelligenceService(
        documents=documents or InMemoryDocumentStore(),
        batches=batches or InMemoryBatchStore(),
        gateway=gateway or InMemoryMadadDocumentGateway(),
        checklist_provider=checklist_provider or InMemoryChecklistProvider(),
        events=events or InMemoryDocumentEventBus(),
        audit=DocumentAuditLogger(),
        config=config,
        sleep=sleep,
        clock=clock,
    )


@lru_cache(maxsize=1)
def get_document_service() -> DocumentIntelligenceService:
    """Process-singleton service; persistence + Madad gateway from settings."""

    from app.core.config import settings

    kwargs: dict[str, Any] = {}
    if settings.persistence.backend == "postgres":
        from app.shared.db.provider import get_database

        from .db import PostgresBatchStore, PostgresDocumentStore

        db = get_database()
        kwargs["documents"] = PostgresDocumentStore(db)
        kwargs["batches"] = PostgresBatchStore(db)
    if settings.mcp.enabled:
        from app.shared.mcp import get_mcp_client

        from .gateways import McpMadadDocumentGateway

        kwargs["gateway"] = McpMadadDocumentGateway(get_mcp_client())
    return build_document_service(**kwargs)
