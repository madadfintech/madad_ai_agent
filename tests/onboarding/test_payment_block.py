"""Payment block (Phase 3): business_details_fetch → products_list_fetch →
payment_create (with idempotency_key) → payment_send_link (with idempotency_key)
→ payment_await. Pin the chain end-to-end."""

from __future__ import annotations

from app.shared.workflow import Channel

WA = Channel.WHATSAPP
IDENTITY = "+97455500701"


async def _drive_to_payment_block(harness):
    """Drive the spec-aligned flow up to the payment_await interrupt."""

    runtime = harness.platform.runtime
    doc = "ZHVtbXk="

    async def resume(message):
        return await runtime.resume(WA, IDENTITY, message=message)

    await runtime.start("onboarding", WA, IDENTITY, input={"trigger": "campaign"})
    await resume({"text": "YES"})
    await resume({"text": "biz@example.com"})  # business_email
    await resume({"attachments": [{"filename": "CR.pdf", "content_base64": doc}]})
    await resume({"attachments": [{"filename": "Audited.pdf", "content_base64": doc}]})
    await resume({"event": "prequalification.completed", "madadScore": 78})
    # Bug #10a + Bug #12 (2026-06-09): one ``madad_score.ready`` event
    # exits the strict docs loop AND fast-forwards through payment_wait
    # into the payment chain — backend only fires it once.
    await resume(
        {
            "attachments": [
                {"filename": "Establishment_Card.pdf", "content_base64": doc}
            ]
        }
    )
    harness.identity.journey_status = "QUALIFIED"
    after_status = await resume(
        {"event": "madad_score.ready", "journey_status": "QUALIFIED"}
    )
    assert after_status.prompt == {"waiting_for": "payment", "step": "payment"}
    return after_status


async def test_payment_chain_runs_in_order(harness):
    await _drive_to_payment_block(harness)

    names = [name for name, _ in harness.payments.calls]
    # UAT 2026-06-19: dropped the side-channel ``send_monetization_payment_link``
    # call. The primary payment-link message goes via our own messenger
    # (CTA-URL + plain-text fallback); the backend's tool returned HTTP 400
    # in every UAT run and added no SME-visible value.
    assert names == [
        "get_business_details",
        "list_monetization_products",
        "create_monetization_payment",
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
    # Format is f"{run_id}:create_monetization_payment" — the run_id prefix
    # makes the key unique per workflow run, while the action suffix is
    # constant so retries reuse the same key.
    assert keys["create_monetization_payment"].endswith(":create_monetization_payment")


async def test_create_payment_idempotency_key_sent_to_tool(harness):
    await _drive_to_payment_block(harness)

    by_name = {name: payload for name, payload in harness.payments.calls}
    create_payload = by_name["create_monetization_payment"]

    assert create_payload["idempotency_key"].endswith(
        ":create_monetization_payment"
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
    # UAT 2026-06-19: the side-channel send_monetization_payment_link MCP
    # call was dropped (always 400 in UAT). The primary payment link
    # still goes via our own messenger; assert the messenger received the
    # WhatsApp CTA-URL keyed on the SME's identity.
    await _drive_to_payment_block(harness)

    cta_sends = [
        s for s in harness.messenger.sent
        if s.get("template_key") == "onboarding.payment.request.button"
        and "cta" in s
    ]
    assert len(cta_sends) == 1
    assert cta_sends[0]["channel"] == Channel.WHATSAPP
    assert cta_sends[0]["identity"] == IDENTITY


async def test_payment_request_template_sent_at_send_link_node(harness):
    await _drive_to_payment_block(harness)

    # The chain sends ONE conversational message at payment_send_link. Main
    # switched to the interactive CTA variant `payment.request.button` (the
    # "Pay QAR 6,000 →" tappable button) instead of plain `payment.request`.
    templates = harness.messenger.templates()
    assert templates.count("onboarding.payment.request.button") == 1
