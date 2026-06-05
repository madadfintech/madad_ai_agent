"""InMemoryMonetizationPaymentClient — protocol conformance + behavioural
expectations including idempotency-key dedupe."""

from __future__ import annotations

from app.services.workflow.ports import (
    InMemoryMonetizationPaymentClient,
    MonetizationPaymentClient,
)
from app.shared.workflow.enums import Channel

TOKEN = "AT-test"


def test_in_memory_payment_client_satisfies_protocol() -> None:
    assert isinstance(InMemoryMonetizationPaymentClient(), MonetizationPaymentClient)


async def test_get_business_details_returns_configured_payload() -> None:
    client = InMemoryMonetizationPaymentClient(
        business_details={"business_details_id": "biz-42", "name": "ACME"}
    )

    out = await client.get_business_details(access_token=TOKEN)

    assert out == {"business_details_id": "biz-42", "name": "ACME"}


async def test_list_monetization_products_returns_default_product() -> None:
    client = InMemoryMonetizationPaymentClient()

    out = await client.list_monetization_products(access_token=TOKEN)

    assert "products" in out
    assert any(p["amount_qar"] == 6000 for p in out["products"])


async def test_create_payment_returns_record_with_idempotency_key() -> None:
    client = InMemoryMonetizationPaymentClient()

    out = await client.create_monetization_payment(
        access_token=TOKEN,
        business_details_id="biz-1",
        product_id="prod-monetization",
        amount_qar=6000,
        idempotency_key="run-1:create_monetization_payment",
    )

    assert out["amount_qar"] == 6000
    assert out["status"] == "CREATED"
    assert out["idempotency_key"] == "run-1:create_monetization_payment"
    assert out["payment_id"].startswith("pay_")


async def test_create_payment_same_key_dedupes_to_same_payment_id() -> None:
    """Backend honours idempotency_key — same key returns the same payment_id
    on a retry, so the workflow keeps using the same record."""

    client = InMemoryMonetizationPaymentClient()
    key = "run-1:create_monetization_payment"

    first = await client.create_monetization_payment(
        access_token=TOKEN,
        business_details_id="biz-1",
        product_id="prod-monetization",
        amount_qar=6000,
        idempotency_key=key,
    )
    second = await client.create_monetization_payment(
        access_token=TOKEN,
        business_details_id="biz-1",
        product_id="prod-monetization",
        amount_qar=6000,
        idempotency_key=key,
    )

    assert first["payment_id"] == second["payment_id"]
    assert len(client.payments) == 1  # exactly one record created


async def test_create_payment_different_keys_create_distinct_payments() -> None:
    client = InMemoryMonetizationPaymentClient()

    first = await client.create_monetization_payment(
        access_token=TOKEN,
        business_details_id="biz-1",
        product_id="prod-monetization",
        amount_qar=6000,
        idempotency_key="run-1:create_monetization_payment",
    )
    second = await client.create_monetization_payment(
        access_token=TOKEN,
        business_details_id="biz-1",
        product_id="prod-monetization",
        amount_qar=6000,
        idempotency_key="run-2:create_monetization_payment",
    )

    assert first["payment_id"] != second["payment_id"]
    assert len(client.payments) == 2


async def test_send_payment_link_attaches_link_to_payment() -> None:
    client = InMemoryMonetizationPaymentClient()

    created = await client.create_monetization_payment(
        access_token=TOKEN,
        business_details_id="biz-1",
        product_id="prod-monetization",
        amount_qar=6000,
        idempotency_key="key-1",
    )
    sent = await client.send_monetization_payment_link(
        access_token=TOKEN,
        payment_id=created["payment_id"],
        channel=Channel.WHATSAPP,
        identity="+97455500001",
        idempotency_key="run-1:send_monetization_payment_link",
    )

    assert sent["payment_link"].startswith("https://pay.madad.example/")
    assert sent["channel"] == str(Channel.WHATSAPP)
    assert client.payments[created["payment_id"]]["payment_link"] == sent["payment_link"]


async def test_get_payment_round_trips_record() -> None:
    client = InMemoryMonetizationPaymentClient()

    created = await client.create_monetization_payment(
        access_token=TOKEN,
        business_details_id="biz-1",
        product_id="prod-monetization",
        amount_qar=6000,
        idempotency_key="key-1",
    )

    fetched = await client.get_monetization_payment(
        access_token=TOKEN, payment_id=created["payment_id"]
    )

    assert fetched["payment_id"] == created["payment_id"]
    assert fetched["status"] == "CREATED"


async def test_sync_status_marks_payment_paid() -> None:
    client = InMemoryMonetizationPaymentClient()

    created = await client.create_monetization_payment(
        access_token=TOKEN,
        business_details_id="biz-1",
        product_id="prod-monetization",
        amount_qar=6000,
        idempotency_key="key-1",
    )
    out = await client.sync_monetization_payment_status(
        access_token=TOKEN, payment_id=created["payment_id"]
    )

    assert out["status"] == "PAID"
    assert client.payments[created["payment_id"]]["status"] == "PAID"


async def test_mark_paid_helper_simulates_webhook_arrival() -> None:
    client = InMemoryMonetizationPaymentClient()

    created = await client.create_monetization_payment(
        access_token=TOKEN,
        business_details_id="biz-1",
        product_id="prod-monetization",
        amount_qar=6000,
        idempotency_key="key-1",
    )
    client.mark_paid(created["payment_id"])

    fetched = await client.get_monetization_payment(
        access_token=TOKEN, payment_id=created["payment_id"]
    )
    assert fetched["status"] == "PAID"
