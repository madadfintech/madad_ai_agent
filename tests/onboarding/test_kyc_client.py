"""InMemoryKycClient — protocol conformance + behavioural expectations."""

from __future__ import annotations

from app.services.workflow.ports import InMemoryKycClient, KycClient

TOKEN = "AT-test"


def test_in_memory_kyc_client_satisfies_protocol() -> None:
    assert isinstance(InMemoryKycClient(), KycClient)


def test_get_admin_requested_documents_returns_missing_subset() -> None:
    client = InMemoryKycClient(required_documents=["trade_license", "tax_card"])

    async def run() -> dict[str, object]:
        return await client.get_admin_requested_documents(access_token=TOKEN)

    import asyncio

    result = asyncio.run(run())
    assert result["required"] == ["trade_license", "tax_card"]
    assert result["missing"] == ["trade_license", "tax_card"]


async def test_upload_document_marks_doc_no_longer_missing() -> None:
    client = InMemoryKycClient(required_documents=["trade_license", "tax_card"])

    await client.upload_document_base64(
        access_token=TOKEN,
        content_base64="abc",
        filename="TL.pdf",
        document_type="trade_license",
    )

    state = await client.get_admin_requested_documents(access_token=TOKEN)
    assert state["missing"] == ["tax_card"]
    assert "trade_license" in client.uploaded_documents


async def test_upload_commercial_registration_records_document() -> None:
    client = InMemoryKycClient()

    result = await client.upload_commercial_registration(
        access_token=TOKEN, content_base64="QkE=", filename="CR.pdf"
    )

    assert result["filename"] == "CR.pdf"
    assert client.cr_document == {
        "filename": "CR.pdf", "content_base64": "QkE=", "mime_type": None,
    }


async def test_upload_audited_financial_report_records_document() -> None:
    client = InMemoryKycClient()

    await client.upload_audited_financial_report(
        access_token=TOKEN, content_base64="QkE=", filename="Audited.pdf"
    )

    assert client.financial_report == {
        "filename": "Audited.pdf",
        "content_base64": "QkE=",
        "mime_type": None,
    }


async def test_upload_document_base64_forwards_client_supplied_mime_type() -> None:
    """If the inbound attachment carried a mime_type (Madad bridge from Meta),
    the client honors it and does not re-infer from filename."""
    client = InMemoryKycClient()

    await client.upload_document_base64(
        access_token=TOKEN,
        content_base64="QkE=",
        filename="trade_license",  # no extension on purpose
        document_type="trade_license",
        mime_type="image/png",
    )

    assert client.uploaded_documents["trade_license"]["mime_type"] == "image/png"


async def test_update_eligibility_returns_configured_payload() -> None:
    expected = {"status": "QUALIFIED", "score": 78}
    client = InMemoryKycClient(eligibility_result=expected)

    out = await client.update_eligibility(
        access_token=TOKEN, data={"annual_revenue_qar": 1_000_000}
    )

    assert out == expected
    # The data payload is recorded for assertion in workflow tests.
    name, payload = client.calls[-1]
    assert name == "update_eligibility"
    assert payload["data"] == {"annual_revenue_qar": 1_000_000}


async def test_add_buyer_returns_record_with_generated_id_and_persists() -> None:
    client = InMemoryKycClient()

    record = await client.add_buyer(
        access_token=TOKEN, data={"name": "ACME LLC", "country": "QA"}
    )

    assert record["name"] == "ACME LLC"
    assert record["buyer_id"].startswith("buyer_")
    assert client.buyers == [record]


async def test_add_shareholders_returns_list_with_generated_ids() -> None:
    client = InMemoryKycClient()

    out = await client.add_shareholders(
        access_token=TOKEN,
        shareholders=[
            {"name": "Aisha", "percentage": 60},
            {"name": "Karim", "percentage": 40},
        ],
    )

    assert len(out["shareholders"]) == 2
    assert {sh["name"] for sh in out["shareholders"]} == {"Aisha", "Karim"}
    for sh in out["shareholders"]:
        assert sh["shareholder_id"].startswith("sh_")
    assert len(client.shareholders) == 2


async def test_every_call_is_recorded_with_access_token_for_introspection() -> None:
    client = InMemoryKycClient(required_documents=["trade_license"])

    await client.upload_commercial_registration(
        access_token=TOKEN, content_base64="x", filename="CR.pdf"
    )
    await client.update_eligibility(access_token=TOKEN, data={})
    await client.get_admin_requested_documents(access_token=TOKEN)

    names = [name for name, _ in client.calls]
    assert names == [
        "upload_commercial_registration",
        "update_eligibility",
        "get_admin_requested_documents",
    ]
    for _, payload in client.calls:
        assert payload["access_token"] == TOKEN
