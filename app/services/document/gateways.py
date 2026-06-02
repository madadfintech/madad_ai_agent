"""Madad document gateway — the single external integration, consumed via MCP.

DATA SOVEREIGNTY: the agentic platform does not run OCR, does not use any external
OCR service, and does NOT store documents or extracted data. Each document is
routed to Madad's existing document-extraction microservice (through the MCP
cluster), which stores the file in Madad's GCP buckets, extracts + classifies +
validates it, and returns a classification, a validity verdict, and a Madad
reference. We retain only that orchestration metadata.

Document bytes, when present (direct uploads or ZIP entries), are passed through
transiently in the MCP call and never persisted by us.
"""

from __future__ import annotations

import base64
from abc import ABC, abstractmethod
from typing import Any

from pydantic import BaseModel, Field

from app.shared.mcp import MCPToolCaller, Tools
from app.shared.workflow.utils import new_id

from .enums import DocumentKind
from .errors import MadadDocumentError


class MadadDocumentResult(BaseModel):
    """What Madad's extraction microservice returns (no document content kept)."""

    document_type: str | None = None  # classification, e.g. "trade_license"
    valid: bool = True
    validation_errors: list[str] = Field(default_factory=list)
    madad_ref: str | None = None  # reference to the stored document in Madad's GCP


class MadadDocumentGateway(ABC):
    """Routes a document to Madad's extraction microservice (store + extract +
    validate). The platform keeps no bytes and no extracted fields."""

    @abstractmethod
    async def process_document(
        self,
        *,
        application_ref: str | None,
        filename: str,
        kind: DocumentKind,
        provider_ref: str | None = None,
        content: bytes | None = None,
    ) -> MadadDocumentResult: ...


class InMemoryMadadDocumentGateway(MadadDocumentGateway):
    """Deterministic fake for tests.

    Classifies by a filename-keyword map, marks documents invalid when the
    filename contains ``invalid``, returns a synthetic Madad reference, and can
    simulate transient routing failures. It records NO document content.
    """

    def __init__(
        self, *, fail_times: int = 0, type_by_keyword: dict[str, str] | None = None
    ) -> None:
        self._fail_times = fail_times
        self._calls = 0
        self._types = type_by_keyword or {}
        self.processed: list[str] = []

    async def process_document(
        self,
        *,
        application_ref: str | None,
        filename: str,
        kind: DocumentKind,
        provider_ref: str | None = None,
        content: bytes | None = None,
    ) -> MadadDocumentResult:
        self._calls += 1
        if self._calls <= self._fail_times:
            raise MadadDocumentError(f"simulated routing failure #{self._calls}")
        lowered = filename.lower()
        document_type = next((t for kw, t in self._types.items() if kw in lowered), None)
        valid = "invalid" not in lowered
        self.processed.append(filename)
        return MadadDocumentResult(
            document_type=document_type,
            valid=valid,
            validation_errors=[] if valid else ["document failed validation"],
            madad_ref=new_id("madoc") if valid else None,
        )


class McpMadadDocumentGateway(MadadDocumentGateway):
    """Routes documents to Madad's extraction microservice via an MCP tool."""

    def __init__(
        self, tool_caller: MCPToolCaller, *, tool: str = Tools.KYC_UPLOAD_DOCUMENT_BASE64
    ) -> None:
        self._tools = tool_caller
        self._tool = tool

    async def process_document(
        self,
        *,
        application_ref: str | None,
        filename: str,
        kind: DocumentKind,
        provider_ref: str | None = None,
        content: bytes | None = None,
    ) -> MadadDocumentResult:
        payload: dict[str, Any] = {
            "application_ref": application_ref,
            "filename": filename,
            "kind": str(kind),
            "provider_ref": provider_ref,
        }
        if content is not None:
            # Transient pass-through; Madad stores it, we do not.
            payload["content_b64"] = base64.b64encode(content).decode("ascii")
        try:
            response = await self._tools.call_tool(self._tool, payload)
        except Exception as exc:  # noqa: BLE001 - normalize transport errors
            raise MadadDocumentError(f"MCP tool {self._tool!r} failed: {exc}") from exc
        return MadadDocumentResult.model_validate(response)
