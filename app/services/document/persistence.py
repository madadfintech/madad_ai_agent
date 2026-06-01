"""Document Intelligence persistence — document + batch stores.

In-memory now; Postgres (orchestration state) lands with the platform DB
foundation. The service depends only on these ports.
"""

from __future__ import annotations

import asyncio
from abc import ABC, abstractmethod

from app.shared.workflow.utils import utcnow

from .models import DocumentBatch, DocumentRecord


class DocumentStore(ABC):
    @abstractmethod
    async def create(self, document: DocumentRecord) -> DocumentRecord: ...

    @abstractmethod
    async def get(self, document_id: str) -> DocumentRecord | None: ...

    @abstractmethod
    async def save(self, document: DocumentRecord) -> DocumentRecord: ...

    @abstractmethod
    async def list_by_batch(self, batch_id: str) -> list[DocumentRecord]: ...

    @abstractmethod
    async def list_by_application(self, application_ref: str) -> list[DocumentRecord]: ...


class BatchStore(ABC):
    @abstractmethod
    async def create(self, batch: DocumentBatch) -> DocumentBatch: ...

    @abstractmethod
    async def get(self, batch_id: str) -> DocumentBatch | None: ...

    @abstractmethod
    async def save(self, batch: DocumentBatch) -> DocumentBatch: ...


class InMemoryDocumentStore(DocumentStore):
    def __init__(self) -> None:
        self._docs: dict[str, DocumentRecord] = {}
        self._lock = asyncio.Lock()

    async def create(self, document: DocumentRecord) -> DocumentRecord:
        async with self._lock:
            self._docs[document.document_id] = document.model_copy(deep=True)
        return document

    async def get(self, document_id: str) -> DocumentRecord | None:
        async with self._lock:
            stored = self._docs.get(document_id)
            return stored.model_copy(deep=True) if stored else None

    async def save(self, document: DocumentRecord) -> DocumentRecord:
        document.updated_at = utcnow()
        async with self._lock:
            self._docs[document.document_id] = document.model_copy(deep=True)
        return document

    async def list_by_batch(self, batch_id: str) -> list[DocumentRecord]:
        async with self._lock:
            docs = [
                d.model_copy(deep=True)
                for d in self._docs.values()
                if d.batch_id == batch_id
            ]
        docs.sort(key=lambda d: d.created_at)
        return docs

    async def list_by_application(self, application_ref: str) -> list[DocumentRecord]:
        async with self._lock:
            docs = [
                d.model_copy(deep=True)
                for d in self._docs.values()
                if d.application_ref == application_ref
            ]
        docs.sort(key=lambda d: d.created_at)
        return docs


class InMemoryBatchStore(BatchStore):
    def __init__(self) -> None:
        self._batches: dict[str, DocumentBatch] = {}
        self._lock = asyncio.Lock()

    async def create(self, batch: DocumentBatch) -> DocumentBatch:
        async with self._lock:
            self._batches[batch.batch_id] = batch.model_copy(deep=True)
        return batch

    async def get(self, batch_id: str) -> DocumentBatch | None:
        async with self._lock:
            stored = self._batches.get(batch_id)
            return stored.model_copy(deep=True) if stored else None

    async def save(self, batch: DocumentBatch) -> DocumentBatch:
        batch.updated_at = utcnow()
        async with self._lock:
            self._batches[batch.batch_id] = batch.model_copy(deep=True)
        return batch
