"""McpInvoiceClient — adapter for the 9 ``madad_invoices_*`` tools.

Phase 1.b. Drives the production adapter against a fake MCP client so
every translation (snake_case → tool argument names, response envelope
normalization, ``invoice_id`` aliasing, ZIP per-file checklist parsing)
is exercised end-to-end.
"""

from __future__ import annotations

from typing import Any

import pytest

from app.services.workflow.mcp_invoices import McpInvoiceClient
from app.shared.mcp import InMemoryMCPClient, Tools


def _client(handlers: dict[str, Any]) -> McpInvoiceClient:
    return McpInvoiceClient(InMemoryMCPClient(handlers=handlers))


@pytest.mark.asyncio
async def test_extract_and_submit_calls_correct_tool_with_required_args() -> None:
    captured: dict[str, Any] = {}

    def handle(args: dict[str, Any]) -> dict[str, Any]:
        captured.update(args)
        return {
            "id": "inv-001",
            "invoice_number": "BLD-123",
            "supplier_name": "Acme Co",
            "customer_name": "Big Buyer LLC",
            "total_amount": 12500,
            "currency": "QAR",
        }

    client = _client({Tools.INVOICES_EXTRACT_AND_SUBMIT_INVOICE_BASE64: handle})
    result = await client.extract_and_submit_base64(
        access_token="tok",
        filename="invoice.pdf",
        content_base64="ZHVtbXk=",
    )

    assert captured["access_token"] == "tok"
    assert captured["file_name"] == "invoice.pdf"
    assert captured["file_base64"] == "ZHVtbXk="
    assert captured["mime_type"] == "application/pdf"
    assert captured["status"] == "UNVERIFIED"
    assert result["invoice_id"] == "inv-001"
    assert result["supplier_name"] == "Acme Co"
    assert result["filename"] == "invoice.pdf"


@pytest.mark.asyncio
async def test_extract_and_submit_unwraps_invoice_envelope() -> None:
    def handle(args: dict[str, Any]) -> dict[str, Any]:
        return {"invoice": {"id": "inv-9", "total_amount": 800}}

    client = _client({Tools.INVOICES_EXTRACT_AND_SUBMIT_INVOICE_BASE64: handle})
    result = await client.extract_and_submit_base64(
        access_token="tok", filename="invoice.pdf", content_base64="ZHVtbXk=",
    )
    assert result["invoice_id"] == "inv-9"
    assert result["total_amount"] == 800


@pytest.mark.asyncio
async def test_extract_and_submit_unwraps_body_envelope() -> None:
    def handle(args: dict[str, Any]) -> dict[str, Any]:
        return {"body": {"id": "inv-7", "total_amount": 400}}

    client = _client({Tools.INVOICES_EXTRACT_AND_SUBMIT_INVOICE_BASE64: handle})
    result = await client.extract_and_submit_base64(
        access_token="tok", filename="invoice.pdf", content_base64="ZHVtbXk=",
    )
    assert result["invoice_id"] == "inv-7"


@pytest.mark.asyncio
async def test_extract_and_submit_threads_user_id_and_status() -> None:
    captured: dict[str, Any] = {}

    def handle(args: dict[str, Any]) -> dict[str, Any]:
        captured.update(args)
        return {}

    client = _client({Tools.INVOICES_EXTRACT_AND_SUBMIT_INVOICE_BASE64: handle})
    await client.extract_and_submit_base64(
        access_token="tok", filename="x.pdf", content_base64="QkE=",
        user_id="user-42", status="VERIFIED",
    )
    assert captured["user_id"] == "user-42"
    assert captured["status"] == "VERIFIED"


@pytest.mark.asyncio
async def test_submit_zip_returns_normalized_checklist() -> None:
    def handle(args: dict[str, Any]) -> dict[str, Any]:
        return {
            "invoices": [
                {"id": "inv-1", "supplier_name": "A", "total_amount": 100},
                {"id": "inv-2", "supplier_name": "B", "total_amount": 200},
            ],
            "failed": 0,
        }

    client = _client({Tools.INVOICES_UPLOAD_ZIP: handle})
    result = await client.submit_zip_base64(
        access_token="tok", filename="bundle.zip", content_base64="ZHVtbXk=",
    )
    assert result["total"] == 2
    assert {i["id"] for i in result["invoices"]} == {"inv-1", "inv-2"}
    assert result["failed"] == 0


@pytest.mark.asyncio
async def test_submit_zip_handles_invoices_only_dict() -> None:
    # Cluster sometimes returns a flat ``{"invoices": [...]}`` without a
    # status_code envelope — adapter normalizes either shape into one.
    def handle(args: dict[str, Any]) -> dict[str, Any]:
        return {"invoices": [{"id": "inv-a"}, {"id": "inv-b"}, {"id": "inv-c"}]}

    client = _client({Tools.INVOICES_UPLOAD_ZIP: handle})
    result = await client.submit_zip_base64(
        access_token="tok", filename="bundle.zip", content_base64="ZHVtbXk=",
    )
    assert result["total"] == 3


@pytest.mark.asyncio
async def test_submit_zip_handles_envelope_with_checklist_key() -> None:
    def handle(args: dict[str, Any]) -> dict[str, Any]:
        return {"body": {"checklist": [{"id": "inv-x"}], "summary": "ok"}}

    client = _client({Tools.INVOICES_UPLOAD_ZIP: handle})
    result = await client.submit_zip_base64(
        access_token="tok", filename="bundle.zip", content_base64="ZHVtbXk=",
    )
    assert result["total"] == 1
    assert result["summary"] == "ok"


@pytest.mark.asyncio
async def test_get_my_invoices_normalizes_to_invoices_dict() -> None:
    # The cluster returns its bare list wrapped in ``{"invoices": [...]}``
    # via the envelope — the adapter reads either shape.
    def handle(args: dict[str, Any]) -> dict[str, Any]:
        return {
            "invoices": [
                {"id": "inv-1", "status": "DISBURSED"},
                {"id": "inv-2", "status": "SUBMITTED"},
            ]
        }

    client = _client({Tools.INVOICES_GET_MY_INVOICES: handle})
    result = await client.get_my_invoices(access_token="tok")
    assert len(result["invoices"]) == 2
    assert result["invoices"][0]["status"] == "DISBURSED"


@pytest.mark.asyncio
async def test_get_my_invoices_handles_body_envelope() -> None:
    def handle(args: dict[str, Any]) -> dict[str, Any]:
        return {"body": [{"id": "inv-7"}]}

    client = _client({Tools.INVOICES_GET_MY_INVOICES: handle})
    result = await client.get_my_invoices(access_token="tok")
    assert result["invoices"][0]["id"] == "inv-7"


@pytest.mark.asyncio
async def test_get_my_invoices_empty_when_response_garbled() -> None:
    def handle(args: dict[str, Any]) -> str:
        return "not a list or dict"

    client = _client({Tools.INVOICES_GET_MY_INVOICES: handle})
    result = await client.get_my_invoices(access_token="tok")
    assert result == {"invoices": []}


@pytest.mark.asyncio
async def test_mime_type_inferred_for_common_extensions() -> None:
    captured: dict[str, Any] = {}

    def handle(args: dict[str, Any]) -> dict[str, Any]:
        captured.update(args)
        return {}

    client = _client({Tools.INVOICES_EXTRACT_AND_SUBMIT_INVOICE_BASE64: handle})
    await client.extract_and_submit_base64(
        access_token="tok", filename="receipt.jpg", content_base64="ZHVtbXk=",
    )
    assert captured["mime_type"] == "image/jpeg"


@pytest.mark.asyncio
async def test_explicit_mime_type_overrides_inference() -> None:
    captured: dict[str, Any] = {}

    def handle(args: dict[str, Any]) -> dict[str, Any]:
        captured.update(args)
        return {}

    client = _client({Tools.INVOICES_EXTRACT_AND_SUBMIT_INVOICE_BASE64: handle})
    await client.extract_and_submit_base64(
        access_token="tok", filename="receipt.bin", content_base64="ZHVtbXk=",
        mime_type="application/octet-stream",
    )
    assert captured["mime_type"] == "application/octet-stream"
