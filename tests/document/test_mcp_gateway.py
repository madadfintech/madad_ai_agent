"""McpMadadDocumentGateway routes documents to Madad's tool (via fake caller)."""

from __future__ import annotations

import base64

import pytest

from app.core.config import McpSettings
from app.services.document.enums import DocumentKind
from app.services.document.errors import MadadDocumentError
from app.services.document.gateways import McpMadadDocumentGateway
from app.shared.mcp import InMemoryMCPClient, Tools


async def test_routes_to_kyc_upload_document_base64_tool_and_parses_result():
    caller = InMemoryMCPClient(
        handlers={
            Tools.KYC_UPLOAD_DOCUMENT_BASE64: lambda p: {
                "document_type": "trade_license",
                "valid": True,
                "madad_ref": "mref_1",
            }
        }
    )
    gateway = McpMadadDocumentGateway(caller)

    result = await gateway.process_document(
        application_ref="app1",
        filename="trade.pdf",
        kind=DocumentKind.ONBOARDING,
        content=b"PDFBYTES",
    )

    name, payload = caller.calls[0]
    assert name == Tools.KYC_UPLOAD_DOCUMENT_BASE64
    assert payload["application_ref"] == "app1"
    assert payload["filename"] == "trade.pdf"
    # Bytes are passed transiently, base64-encoded (Madad stores, we do not).
    assert base64.b64decode(payload["content_b64"]) == b"PDFBYTES"
    assert result.document_type == "trade_license"
    assert result.madad_ref == "mref_1"


async def test_transport_failure_is_normalised_to_madad_error():
    caller = InMemoryMCPClient(fail_times=1, settings=McpSettings(retry_max_attempts=1))
    gateway = McpMadadDocumentGateway(caller)

    with pytest.raises(MadadDocumentError):
        await gateway.process_document(
            application_ref="app1", filename="x.pdf", kind=DocumentKind.ONBOARDING
        )
