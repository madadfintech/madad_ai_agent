"""Document Intelligence Service FastAPI app (Application Server container, port 8005).

Uploaded bytes are forwarded transiently to Madad's extraction microservice and
never stored by us (data sovereignty). In production, channel documents arrive as
a ``provider_ref`` that Madad fetches.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Annotated

from fastapi import Depends, FastAPI, File, Form, UploadFile

from app.core.app import create_service_app
from app.shared.events import connect_forwarders, get_event_bus

from .checklist import ChecklistStatus
from .deps import get_document_service
from .enums import DocumentKind
from .schemas import BatchDTO, DocumentDTO
from .service import DocumentIntelligenceService


@asynccontextmanager
async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
    connect_forwarders(get_event_bus(), document=get_document_service().events)
    yield


app = create_service_app(
    title="MADAD Document Intelligence Service", service="document", lifespan=lifespan
)

Service = Annotated[DocumentIntelligenceService, Depends(get_document_service)]


@app.post("/documents/upload", response_model=DocumentDTO)
async def upload(
    service: Service,
    file: Annotated[UploadFile, File()],
    application_ref: Annotated[str | None, Form()] = None,
    kind: Annotated[DocumentKind, Form()] = DocumentKind.ONBOARDING,
) -> DocumentDTO:
    content = await file.read()  # forwarded to Madad transiently, not stored
    document = await service.ingest_document(
        file.filename or "upload.bin",
        application_ref=application_ref,
        kind=kind,
        content=content,
    )
    return DocumentDTO.from_model(document)


@app.post("/documents/zip", response_model=BatchDTO)
async def upload_zip(
    service: Service,
    file: Annotated[UploadFile, File()],
    application_ref: Annotated[str | None, Form()] = None,
    kind: Annotated[DocumentKind, Form()] = DocumentKind.ONBOARDING,
    checklist: Annotated[str | None, Form()] = None,
) -> BatchDTO:
    content = await file.read()  # unpacked in memory; entries forwarded, not stored
    batch = await service.ingest_zip(
        file.filename or "upload.zip",
        content,
        application_ref=application_ref,
        kind=kind,
        checklist=checklist,
    )
    documents = await service.list_batch_documents(batch.batch_id)
    return BatchDTO.from_model(batch, documents)


@app.get("/documents/batches/{batch_id}", response_model=BatchDTO)
async def get_batch(batch_id: str, service: Service) -> BatchDTO:
    batch = await service.get_batch(batch_id)
    documents = await service.list_batch_documents(batch_id)
    return BatchDTO.from_model(batch, documents)


@app.get("/documents/checklist/{checklist}", response_model=ChecklistStatus)
async def checklist_status(
    checklist: str,
    service: Service,
    application_ref: str | None = None,
    batch_id: str | None = None,
) -> ChecklistStatus:
    return await service.checklist_status(
        checklist, application_ref=application_ref, batch_id=batch_id
    )


@app.get("/documents/{document_id}", response_model=DocumentDTO)
async def get_document(document_id: str, service: Service) -> DocumentDTO:
    document = await service.get_document(document_id)
    return DocumentDTO.from_model(document)
