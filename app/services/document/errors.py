"""Document Intelligence exception hierarchy."""

from __future__ import annotations

from app.core.exceptions import AppError


class DocumentError(AppError):
    code = "document_error"


class DocumentNotFoundError(DocumentError):
    code = "document_not_found"
    http_status = 404


class BatchNotFoundError(DocumentError):
    code = "document_batch_not_found"
    http_status = 404


class MadadDocumentError(DocumentError):
    """A transient failure routing a document to Madad's extraction microservice."""

    code = "madad_document_error"


class ZipExtractionError(DocumentError):
    code = "document_zip_error"
