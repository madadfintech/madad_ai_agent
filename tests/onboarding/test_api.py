"""API-level test: drive the full onboarding flow over HTTP."""

from __future__ import annotations

from fastapi.testclient import TestClient

from app.services.workflow.main import app

client = TestClient(app)
IDENTITY = "+97455509000"


def _start():
    return client.post(
        "/workflow/campaign/start", json={"channel": "whatsapp", "identity": IDENTITY}
    )


def _inbound(**body):
    return client.post(
        "/workflow/inbound", json={"channel": "whatsapp", "identity": IDENTITY, **body}
    )


def _madad_status(event, payload):
    return client.post(
        f"/workflow/madad/status/{event}",
        json={"channel": "whatsapp", "identity": IDENTITY, "payload": payload},
    )


def _offer_acceptance(offer_id):
    return client.post(
        "/workflow/webhooks/offer-acceptance",
        json={"channel": "whatsapp", "identity": IDENTITY, "offer_id": offer_id},
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

    assert _inbound(text="YES").json()["prompt"]["step"] == "consent_cr"
    assert _inbound(attachments=[{"filename": "CR.pdf"}]).json()["prompt"]["step"] == "financials"
    assert (
        _inbound(attachments=[{"filename": "Audited.pdf"}]).json()["prompt"]["step"]
        == "prequalification"
    )

    status = client.get("/workflow/status", params={"channel": "whatsapp", "identity": IDENTITY})
    assert status.status_code == 200
    assert status.json()["status"] == "waiting_for_input"

    # Async financing decisions arrive as Madad backend status callbacks.
    assert (
        _madad_status("prequalification", {"qualified": True}).json()["prompt"]["step"]
        == "documents"
    )
    docs = _inbound(
        attachments=[{"filename": "Trade.pdf"}, {"filename": "Tax.pdf"}, {"filename": "Bank.pdf"}]
    )
    assert docs.json()["prompt"]["step"] == "risk_assessment"
    assert (
        _madad_status("score", {"score": 78, "qualified": True}).json()["prompt"]["step"]
        == "payment"
    )
    assert _madad_status("payment", {"paid": True}).json()["prompt"]["step"] == "lender_evaluation"
    assert (
        _madad_status("offers", {"offers": [{"offer_id": "o1"}]}).json()["prompt"]["step"]
        == "offer_preview"
    )

    # Offer acceptance is the one event that comes back via the webhook receiver.
    final = _offer_acceptance("o1")
    assert final.json()["completed"] is True
    assert final.json()["outcome"] == "completed"


def test_unknown_status_event_rejected() -> None:
    response = client.post(
        "/workflow/madad/status/nonsense",
        json={"channel": "whatsapp", "identity": "+97455509999", "payload": {}},
    )
    assert response.status_code == 400
