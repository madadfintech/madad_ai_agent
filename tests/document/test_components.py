"""Unit tests for the Madad document gateway (fake)."""

from __future__ import annotations

import pytest

from app.services.document import (
    DocumentKind,
    InMemoryMadadDocumentGateway,
    MadadDocumentError,
)


async def test_gateway_classifies_and_returns_ref():
    gateway = InMemoryMadadDocumentGateway(type_by_keyword={"trade": "trade_license"})
    result = await gateway.process_document(
        application_ref="A", filename="Trade.pdf", kind=DocumentKind.ONBOARDING, content=b"x"
    )
    assert result.document_type == "trade_license"
    assert result.valid is True
    assert result.madad_ref is not None


async def test_gateway_rejects_invalid():
    gateway = InMemoryMadadDocumentGateway()
    result = await gateway.process_document(
        application_ref="A", filename="invalid.pdf", kind=DocumentKind.ONBOARDING
    )
    assert result.valid is False
    assert result.madad_ref is None


async def test_gateway_transient_failure():
    gateway = InMemoryMadadDocumentGateway(fail_times=1)
    with pytest.raises(MadadDocumentError):
        await gateway.process_document(
            application_ref="A", filename="x.pdf", kind=DocumentKind.ONBOARDING
        )
