"""McpKycClient — exercises every method against a fake MCP caller."""

from __future__ import annotations

from app.services.workflow.mcp_kyc import McpKycClient
from app.services.workflow.ports import KycClient
from app.shared.mcp import InMemoryMCPClient, Tools

TOKEN = "AT-123"


def test_mcp_kyc_adapter_satisfies_protocol() -> None:
    assert isinstance(McpKycClient(InMemoryMCPClient()), KycClient)


async def test_upload_commercial_registration_calls_correct_tool() -> None:
    caller = InMemoryMCPClient(
        handlers={
            Tools.KYC_UPLOAD_COMMERCIAL_REGISTRATION: lambda p: {
                "document_id": "doc-cr-1",
                "filename": p["filename"],
            }
        }
    )

    out = await McpKycClient(caller).upload_commercial_registration(
        access_token=TOKEN, content_base64="QkE=", filename="CR.pdf"
    )

    assert out == {"document_id": "doc-cr-1", "filename": "CR.pdf"}
    name, payload = caller.calls[0]
    assert name == Tools.KYC_UPLOAD_COMMERCIAL_REGISTRATION
    assert payload == {
        "access_token": TOKEN,
        "content_base64": "QkE=",
        "filename": "CR.pdf",
    }


async def test_update_eligibility_merges_data_into_payload() -> None:
    caller = InMemoryMCPClient(
        handlers={Tools.KYC_UPDATE_ELIGIBILITY: lambda p: {"status": "QUALIFIED"}}
    )

    out = await McpKycClient(caller).update_eligibility(
        access_token=TOKEN,
        data={"annual_revenue_qar": 5_000_000, "sector": "trade"},
    )

    assert out == {"status": "QUALIFIED"}
    _, payload = caller.calls[0]
    assert payload == {
        "access_token": TOKEN,
        "annual_revenue_qar": 5_000_000,
        "sector": "trade",
    }


async def test_upload_audited_financial_report_calls_correct_tool() -> None:
    caller = InMemoryMCPClient(
        handlers={Tools.KYC_UPLOAD_AUDITED_FINANCIAL_REPORT: lambda p: {"document_id": "fr-1"}}
    )

    await McpKycClient(caller).upload_audited_financial_report(
        access_token=TOKEN, content_base64="QkE=", filename="Audited.pdf"
    )

    name, payload = caller.calls[0]
    assert name == Tools.KYC_UPLOAD_AUDITED_FINANCIAL_REPORT
    assert payload["filename"] == "Audited.pdf"
    assert payload["access_token"] == TOKEN


async def test_get_admin_requested_documents_sends_only_access_token() -> None:
    caller = InMemoryMCPClient(
        handlers={
            Tools.KYC_GET_ADMIN_REQUESTED_DOCUMENTS: lambda p: {
                "required": ["trade_license", "tax_card"],
                "missing": ["tax_card"],
            }
        }
    )

    out = await McpKycClient(caller).get_admin_requested_documents(access_token=TOKEN)

    assert out["missing"] == ["tax_card"]
    _, payload = caller.calls[0]
    assert payload == {"access_token": TOKEN}


async def test_upload_document_base64_includes_document_type() -> None:
    caller = InMemoryMCPClient(
        handlers={
            Tools.KYC_UPLOAD_DOCUMENT_BASE64: lambda p: {"document_id": "doc-1"}
        }
    )

    await McpKycClient(caller).upload_document_base64(
        access_token=TOKEN,
        content_base64="QkE=",
        filename="TL.pdf",
        document_type="trade_license",
    )

    _, payload = caller.calls[0]
    assert payload == {
        "access_token": TOKEN,
        "content_base64": "QkE=",
        "filename": "TL.pdf",
        "document_type": "trade_license",
    }


async def test_add_buyer_merges_data_with_access_token() -> None:
    caller = InMemoryMCPClient(
        handlers={Tools.KYC_ADD_BUYER: lambda p: {"buyer_id": "b-1", **p}}
    )

    out = await McpKycClient(caller).add_buyer(
        access_token=TOKEN, data={"name": "ACME LLC", "country": "QA"}
    )

    assert out["buyer_id"] == "b-1"
    _, payload = caller.calls[0]
    assert payload == {"access_token": TOKEN, "name": "ACME LLC", "country": "QA"}


async def test_add_shareholders_passes_list_through() -> None:
    caller = InMemoryMCPClient(
        handlers={
            Tools.KYC_ADD_SHAREHOLDERS: lambda p: {
                "shareholders": [{"shareholder_id": "sh-1", **sh} for sh in p["shareholders"]]
            }
        }
    )

    shareholders = [{"name": "Aisha", "percentage": 60}, {"name": "Karim", "percentage": 40}]
    out = await McpKycClient(caller).add_shareholders(
        access_token=TOKEN, shareholders=shareholders
    )

    assert len(out["shareholders"]) == 2
    _, payload = caller.calls[0]
    assert payload == {"access_token": TOKEN, "shareholders": shareholders}
