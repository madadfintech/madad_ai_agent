"""PostgreSQL-backed document stores (document + batch).

Holds ONLY orchestration metadata (no document bytes, no extracted fields) — see
the data-sovereignty design. The ``data`` JSON is the slim ``DocumentRecord``.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import String, select
from sqlalchemy.orm import Mapped, mapped_column

from app.shared.db import Base, Database

from .models import DocumentBatch, DocumentRecord
from .persistence import BatchStore, DocumentStore


class DocumentRow(Base):
    __tablename__ = "documents"
    __table_args__ = {"schema": "document"}

    document_id: Mapped[str] = mapped_column(String, primary_key=True)
    batch_id: Mapped[str | None] = mapped_column(String, nullable=True, index=True)
    application_ref: Mapped[str | None] = mapped_column(String, nullable=True, index=True)
    status: Mapped[str] = mapped_column(String, index=True)
    created_at: Mapped[str] = mapped_column(String, index=True)
    data: Mapped[dict[str, Any]] = mapped_column()


class DocumentBatchRow(Base):
    __tablename__ = "batches"
    __table_args__ = {"schema": "document"}

    batch_id: Mapped[str] = mapped_column(String, primary_key=True)
    data: Mapped[dict[str, Any]] = mapped_column()


class PostgresDocumentStore(DocumentStore):
    def __init__(self, db: Database) -> None:
        self._db = db

    async def create(self, document: DocumentRecord) -> DocumentRecord:
        async with self._db.session() as session:
            session.add(_document_row(document))
        return document

    async def get(self, document_id: str) -> DocumentRecord | None:
        async with self._db.session() as session:
            row = await session.get(DocumentRow, document_id)
            return DocumentRecord.model_validate(row.data) if row else None

    async def save(self, document: DocumentRecord) -> DocumentRecord:
        from app.shared.workflow.utils import utcnow

        document.updated_at = utcnow()
        async with self._db.session() as session:
            row = await session.get(DocumentRow, document.document_id)
            if row is None:
                session.add(_document_row(document))
            else:
                row.status = str(document.status)
                row.data = document.model_dump(mode="json")
        return document

    async def list_by_batch(self, batch_id: str) -> list[DocumentRecord]:
        return await self._list(DocumentRow.batch_id == batch_id)

    async def list_by_application(self, application_ref: str) -> list[DocumentRecord]:
        return await self._list(DocumentRow.application_ref == application_ref)

    async def _list(self, where: Any) -> list[DocumentRecord]:
        async with self._db.session() as session:
            rows = (
                (
                    await session.execute(
                        select(DocumentRow).where(where).order_by(DocumentRow.created_at)
                    )
                )
                .scalars()
                .all()
            )
            return [DocumentRecord.model_validate(r.data) for r in rows]


class PostgresBatchStore(BatchStore):
    def __init__(self, db: Database) -> None:
        self._db = db

    async def create(self, batch: DocumentBatch) -> DocumentBatch:
        async with self._db.session() as session:
            session.add(
                DocumentBatchRow(batch_id=batch.batch_id, data=batch.model_dump(mode="json"))
            )
        return batch

    async def get(self, batch_id: str) -> DocumentBatch | None:
        async with self._db.session() as session:
            row = await session.get(DocumentBatchRow, batch_id)
            return DocumentBatch.model_validate(row.data) if row else None

    async def save(self, batch: DocumentBatch) -> DocumentBatch:
        from app.shared.workflow.utils import utcnow

        batch.updated_at = utcnow()
        async with self._db.session() as session:
            row = await session.get(DocumentBatchRow, batch.batch_id)
            if row is None:
                session.add(
                    DocumentBatchRow(batch_id=batch.batch_id, data=batch.model_dump(mode="json"))
                )
            else:
                row.data = batch.model_dump(mode="json")
        return batch


def _document_row(document: DocumentRecord) -> DocumentRow:
    return DocumentRow(
        document_id=document.document_id,
        batch_id=document.batch_id,
        application_ref=document.application_ref,
        status=str(document.status),
        created_at=document.created_at.isoformat(),
        data=document.model_dump(mode="json"),
    )
