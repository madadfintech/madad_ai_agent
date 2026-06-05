"""McpMonetizationPaymentAdapter + McpTessLoanPaymentAdapter — every method
hits the right Tools.* constant with the right payload shape; Tess stub
fails loudly at the seam."""

from __future__ import annotations

import pytest

from app.services.workflow.mcp_payments import (
    McpMonetizationPaymentAdapter,
    McpTessLoanPaymentAdapter,
)
from app.services.workflow.ports import MonetizationPaymentClient
from app.shared.mcp import InMemoryMCPClient, Tools
from app.shared.workflow.enums import Channel

TOKEN = "AT-123"


def test_mcp_payment_adapter_satisfies_protocol() -> None:
    assert isinstance(
        McpMonetizationPaymentAdapter(InMemoryMCPClient()), MonetizationPaymentClient
    )


async def test_get_business_details_calls_kyc_tool() -> None:
    caller = InMemoryMCPClient(
        handlers={
            Tools.KYC_GET_BUSINESS_DETAILS: lambda p: {
                "business_details_id": "biz-42",
                "name": "ACME",
            }
        }
    )

    out = await McpMonetizationPaymentAdapter(caller).get_business_details(
        access_token=TOKEN
    )

    assert out["business_details_id"] == "biz-42"
    name, payload = caller.calls[0]
    assert name == Tools.KYC_GET_BUSINESS_DETAILS
    assert payload == {"access_token": TOKEN}


async def test_list_monetization_products_calls_payments_tool() -> None:
    caller = InMemoryMCPClient(
        handlers={
            Tools.PAYMENTS_LIST_MONETIZATION_PRODUCTS: lambda p: {
                "products": [{"product_id": "p1", "amount_qar": 6000}]
            }
        }
    )

    out = await McpMonetizationPaymentAdapter(caller).list_monetization_products(
        access_token=TOKEN
    )

    assert out["products"][0]["amount_qar"] == 6000
    name, payload = caller.calls[0]
    assert name == Tools.PAYMENTS_LIST_MONETIZATION_PRODUCTS
    assert payload == {"access_token": TOKEN}


async def test_create_monetization_payment_threads_idempotency_key() -> None:
    caller = InMemoryMCPClient(
        handlers={
            Tools.PAYMENTS_CREATE_MONETIZATION_PAYMENT: lambda p: {
                "payment_id": "pay-1",
                "status": "CREATED",
            }
        }
    )

    out = await McpMonetizationPaymentAdapter(caller).create_monetization_payment(
        access_token=TOKEN,
        business_details_id="biz-1",
        product_id="prod-monetization",
        amount_qar=6000,
        idempotency_key="run-1:create_monetization_payment",
    )

    assert out["payment_id"] == "pay-1"
    name, payload = caller.calls[0]
    assert name == Tools.PAYMENTS_CREATE_MONETIZATION_PAYMENT
    assert payload == {
        "access_token": TOKEN,
        "business_details_id": "biz-1",
        "product_id": "prod-monetization",
        "amount_qar": 6000,
        "idempotency_key": "run-1:create_monetization_payment",
    }


async def test_send_monetization_payment_link_uppercase_channel_and_key() -> None:
    caller = InMemoryMCPClient(
        handlers={
            Tools.PAYMENTS_SEND_MONETIZATION_PAYMENT_LINK: lambda p: {
                "payment_link": "https://pay.madad.example/x"
            }
        }
    )

    out = await McpMonetizationPaymentAdapter(
        caller
    ).send_monetization_payment_link(
        access_token=TOKEN,
        payment_id="pay-1",
        channel=Channel.WHATSAPP,
        identity="+97455500001",
        idempotency_key="run-1:send_monetization_payment_link",
    )

    assert out["payment_link"] == "https://pay.madad.example/x"
    _, payload = caller.calls[0]
    assert payload == {
        "access_token": TOKEN,
        "payment_id": "pay-1",
        "channel": "WHATSAPP",
        "identity": "+97455500001",
        "idempotency_key": "run-1:send_monetization_payment_link",
    }


async def test_get_monetization_payment_passes_payment_id() -> None:
    caller = InMemoryMCPClient(
        handlers={
            Tools.PAYMENTS_GET_MONETIZATION_PAYMENT: lambda p: {
                "payment_id": p["payment_id"],
                "status": "PAID",
            }
        }
    )

    out = await McpMonetizationPaymentAdapter(caller).get_monetization_payment(
        access_token=TOKEN, payment_id="pay-1"
    )

    assert out == {"payment_id": "pay-1", "status": "PAID"}
    _, payload = caller.calls[0]
    assert payload == {"access_token": TOKEN, "payment_id": "pay-1"}


async def test_sync_monetization_payment_status_passes_payment_id() -> None:
    caller = InMemoryMCPClient(
        handlers={
            Tools.PAYMENTS_SYNC_MONETIZATION_PAYMENT_STATUS: lambda p: {"status": "PAID"}
        }
    )

    out = await McpMonetizationPaymentAdapter(
        caller
    ).sync_monetization_payment_status(access_token=TOKEN, payment_id="pay-1")

    assert out["status"] == "PAID"
    _, payload = caller.calls[0]
    assert payload == {"access_token": TOKEN, "payment_id": "pay-1"}


# -- Tess loan stub ----------------------------------------------------------


async def test_tess_loan_adapter_raises_with_pending_q5_message() -> None:
    stub = McpTessLoanPaymentAdapter(InMemoryMCPClient())

    with pytest.raises(NotImplementedError) as exc_info:
        await stub.disburse_loan(access_token=TOKEN, application_id="app-1")

    assert "Tess loan-payment MCP tools not yet shipped" in str(exc_info.value)
    assert "Q5" in str(exc_info.value)
