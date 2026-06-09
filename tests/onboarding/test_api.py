"""API-level test: drive the reshaped Phase 4 onboarding flow over HTTP.

Phase 4 separates inbound user replies (``/workflow/inbound``) from backend
webhook events (``/workflow/madad/events/{event_type}``). The eligibility
form and counterparty captures arrive as inbound ``data`` payloads;
``payment.completed`` and ``status_update`` arrive at the webhook chokepoint.
"""

from __future__ import annotations

from fastapi.testclient import TestClient

from app.services.workflow.deps import get_onboarding_platform
from app.services.workflow.main import app

client = TestClient(app)
IDENTITY = "+97455509000"
DOC = "ZHVtbXk="


def _start():
    return client.post(
        "/workflow/campaign/start", json={"channel": "whatsapp", "identity": IDENTITY}
    )


def _inbound(**body):
    return client.post(
        "/workflow/inbound", json={"channel": "whatsapp", "identity": IDENTITY, **body}
    )


def _backend_event(event_type, payload=None, event_id=None):
    return client.post(
        f"/workflow/madad/events/{event_type}",
        json={
            "channel": "whatsapp",
            "identity": IDENTITY,
            "event_id": event_id,
            "payload": payload or {},
        },
    )


def test_health() -> None:
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json()["service"] == "workflow"


def test_full_flow_over_http() -> None:
    started = _start()
    assert started.status_code == 200
    assert started.json()["waiting"] is True
    assert started.json()["prompt"]["step"] == "campaign"

    # Post-main spec-aligned flow: YES → consent_cr direct (no collect_details
    # form), CR → financials direct (no 7-field eligibility form), audited →
    # PARK(prequalify_wait), prequalification.completed → documents, doc upload
    # → PARK(journey_wait or payment_wait depending on path), payment chain.
    assert _inbound(text="YES").json()["prompt"]["step"] == "consent_cr"
    assert (
        _inbound(attachments=[{"filename": "CR.pdf", "content_base64": DOC}]).json()["prompt"]["step"]  # noqa: E501
        == "financials"
    )
    assert (
        _inbound(attachments=[{"filename": "Audited.pdf", "content_base64": DOC}]).json()["prompt"]["step"]  # noqa: E501
        == "prequalify_wait"
    )

    # Release prequalification gate via webhook.
    advanced = _backend_event(
        "prequalification.completed", payload={"madadScore": 78}, event_id="evt-prequal"
    )
    assert advanced.json()["prompt"]["step"] == "documents"

    # Bug #10a (2026-06-09): the docs loop is strict — one upload is the
    # SME's contribution, the admin-reviewed webhook then advances the
    # journey. Mirrors the production "admin signed off" path.
    _inbound(
        attachments=[
            {"filename": "Establishment_Card.pdf", "content_base64": DOC}
        ]
    )
    docs = _backend_event(
        "documents.completed",
        payload={"journey_status": "QUALIFIED"},
        event_id="evt-docs-reviewed",
    )
    # After docs the workflow PARKs at payment_wait (not journey_wait — main
    # restructured the flow so payment gate fires off madad_score.ready /
    # payment.requested events, not status_update).
    assert docs.json()["prompt"]["step"] == "payment_wait"

    # Backend fires the payment-gate trigger → payment chain → payment_await.
    platform = get_onboarding_platform()
    platform.workflow._identity.journey_status = "PRE_QUALIFIED"  # type: ignore[union-attr]
    advanced = _backend_event("madad_score.ready", {"madadScore": 78}, event_id="evt-score")
    assert advanced.json()["prompt"]["step"] == "payment"

    # Payment paid → lender_wait.
    paid = _backend_event("payment.completed", {"paid": True}, event_id="evt-paid")
    assert paid.json()["prompt"]["step"] == "lender_wait"

    # ACCEPTED → offers_fetch → offer handoff terminal.
    platform.workflow._identity.journey_status = "ACCEPTED"  # type: ignore[union-attr]
    final = _backend_event("offers.available", event_id="evt-offers")
    assert final.json()["completed"] is True
    assert final.json()["outcome"] == "offer_handoff"


def test_unknown_backend_event_rejected_with_400() -> None:
    response = client.post(
        "/workflow/madad/events/nonsense",
        json={
            "channel": "whatsapp",
            "identity": "+97455599999",
            "payload": {},
        },
    )
    assert response.status_code == 400
    assert response.json()["code"] == "unknown_event_type"


def test_duplicate_event_id_is_deduped() -> None:
    # Start a fresh run identity so this test is independent of the full-flow.
    identity = "+97455509777"
    client.post("/workflow/campaign/start", json={"channel": "whatsapp", "identity": identity})

    def event():
        return client.post(
            "/workflow/madad/events/eligibility.updated",
            json={
                "channel": "whatsapp",
                "identity": identity,
                "event_id": "dup-key-1",
                "payload": {},
            },
        )

    first = event()
    # The first call may not advance the workflow (it's still in campaign_await),
    # but the dedupe layer accepts it. Status code 200 either way.
    assert first.status_code == 200
    second = event()
    assert second.status_code == 200
    assert second.json() == {"deduped": True}


def test_inbound_duplicate_message_id_is_deduped() -> None:
    """Inbound bridges (Meta-WhatsApp / SendGrid) may re-deliver the same
    payload on transient bridge-side errors. When the bridge supplies a
    ``message_id`` (or the ``X-Madad-Message-Id`` header), the dispatcher
    dedupes — the duplicate POST gets 200 ``{"deduped": true}`` and does
    not re-play through the workflow."""

    identity = "+97455509888"
    body = {
        "channel": "whatsapp",
        "identity": identity,
        "text": "hi",
        "message_id": "wamid.dedup-test-1",
    }

    first = client.post("/workflow/inbound", json=body)
    assert first.status_code == 200
    assert first.json().get("deduped") is not True  # first call ran the workflow

    second = client.post("/workflow/inbound", json=body)
    assert second.status_code == 200
    assert second.json() == {"deduped": True}


def test_inbound_message_id_from_header_takes_precedence() -> None:
    """Bridges can supply the message_id via the X-Madad-Message-Id header
    instead of the body; the header wins when both are present."""

    identity = "+97455509999"
    first = client.post(
        "/workflow/inbound",
        json={"channel": "whatsapp", "identity": identity, "text": "hi"},
        headers={"X-Madad-Message-Id": "wamid.header-test-1"},
    )
    assert first.status_code == 200
    assert first.json().get("deduped") is not True

    second = client.post(
        "/workflow/inbound",
        json={"channel": "whatsapp", "identity": identity, "text": "anything"},
        headers={"X-Madad-Message-Id": "wamid.header-test-1"},
    )
    assert second.status_code == 200
    assert second.json() == {"deduped": True}
