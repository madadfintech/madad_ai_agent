"""Payment block (Phase 3): business_details_fetch → products_list_fetch →
payment_create (with idempotency_key) → payment_send_link (with idempotency_key)
→ payment_await. Pin the chain end-to-end."""

from __future__ import annotations

from app.shared.workflow import Channel

WA = Channel.WHATSAPP
IDENTITY = "+97455500701"


async def _drive_to_payment_block(harness):
    """Drive the workflow up to and through the payment chain (paid)."""

    runtime = harness.platform.runtime

    async def resume(message):
        return await runtime.resume(WA, IDENTITY, message=message)

    await runtime.start("onboarding", WA, IDENTITY, input={"trigger": "campaign"})
    await resume({"text": "YES"})
    await resume({"first_name": "A", "last_name": "B"})
    await resume({"attachments": [{"filename": "CR.pdf"}]})
    await resume({"annual_revenue_qar": 1000})
    await resume({"attachments": [{"filename": "Audited.pdf"}]})
    await resume({"name": "Buyer 1"})
    await resume({"shareholders": [{"name": "A", "percentage": 100}]})
    await resume(
        {"attachments": [{"filename": "Trade_License.pdf"}, {"filename": "Tax_Card.pdf"}]}
    )

    harness.identity.journey_status = "PRE_QUALIFIED"
    after_status = await resume({"type": "status_update"})
    # After the status update the chain has already run silently up to the
    # payment_await await — the prompt confirms we're at the payment-wait
    # interrupt.
    assert after_status.prompt == {"waiting_for": "payment", "step": "payment"}
    return after_status


async def test_payment_chain_runs_in_order(harness):
    await _drive_to_payment_block(harness)

    names = [name for name, _ in harness.payments.calls]
    # First get_business_details happens during _eligibility_update (state
    # syncs the backend's normalized eligibility values back into state);
    # the second + rest are the payment chain proper.
    assert names == [
        "get_business_details",                # from eligibility_update state-sync
        "get_business_details",                # from business_details_fetch node
        "list_monetization_products",
        "create_monetization_payment",
        "send_monetization_payment_link",
    ]


async def test_state_populated_from_each_chain_step(harness):
    after_status = await _drive_to_payment_block(harness)

    # Pull the post-chain values from the run state.
    state = after_status.values

    assert state["business_details_id"] == "biz-1"  # InMemory default
    assert state["payment_product_id"] == "prod-monetization"
    assert state["payment_id"].startswith("pay_")
    assert state["payment_link"].startswith("https://pay.madad.example/")
    assert state["payment_status"] == "CREATED"


async def test_idempotency_keys_recorded_in_state(harness):
    after_status = await _drive_to_payment_block(harness)
    keys = after_status.values["idempotency_keys"]

    assert "create_monetization_payment" in keys
    assert "send_monetization_payment_link" in keys
    # Format is f"{run_id}:create_monetization_payment" — the run_id prefix
    # makes the key unique per workflow run, while the action suffix is
    # constant so retries reuse the same key.
    assert keys["create_monetization_payment"].endswith(":create_monetization_payment")
    assert keys["send_monetization_payment_link"].endswith(
        ":send_monetization_payment_link"
    )


async def test_idempotency_keys_sent_to_create_and_send_link_tools(harness):
    await _drive_to_payment_block(harness)

    by_name = {name: payload for name, payload in harness.payments.calls}
    create_payload = by_name["create_monetization_payment"]
    send_payload = by_name["send_monetization_payment_link"]

    assert create_payload["idempotency_key"].endswith(
        ":create_monetization_payment"
    )
    assert send_payload["idempotency_key"].endswith(
        ":send_monetization_payment_link"
    )
    # And the keys differ per write so backend dedupe is per-write, not joint.
    assert (
        create_payload["idempotency_key"] != send_payload["idempotency_key"]
    )


async def test_payment_create_uses_business_details_and_product_from_prior_steps(
    harness,
):
    await _drive_to_payment_block(harness)

    by_name = {name: payload for name, payload in harness.payments.calls}
    create = by_name["create_monetization_payment"]

    assert create["business_details_id"] == "biz-1"
    assert create["product_id"] == "prod-monetization"
    assert create["amount_qar"] == 6000


async def test_send_link_uses_channel_and_identity_from_context(harness):
    await _drive_to_payment_block(harness)

    by_name = {name: payload for name, payload in harness.payments.calls}
    send = by_name["send_monetization_payment_link"]

    assert send["channel"] == Channel.WHATSAPP
    assert send["identity"] == IDENTITY


async def test_payment_request_template_sent_at_send_link_node(harness):
    await _drive_to_payment_block(harness)

    # The chain only sends ONE conversational message — the introductory
    # "your payment is ready" template at payment_send_link. The MCP tool
    # itself delivers the actual link via the channel from the Madad
    # backend (we don't duplicate-send).
    templates = harness.messenger.templates()
    assert templates.count("onboarding.payment.request") == 1
