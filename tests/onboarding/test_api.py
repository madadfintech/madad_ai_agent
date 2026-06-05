"""API-level test: drive the reshaped Phase 2 onboarding flow over HTTP."""

from __future__ import annotations

from fastapi.testclient import TestClient

from app.services.workflow.deps import get_onboarding_platform
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


def test_health() -> None:
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json()["service"] == "workflow"


def test_full_flow_over_http() -> None:
    started = _start()
    assert started.status_code == 200
    assert started.json()["waiting"] is True
    assert started.json()["prompt"]["step"] == "campaign"

    assert _inbound(text="YES").json()["prompt"]["step"] == "collect_details"
    assert (
        client.post(
            "/workflow/inbound",
            json={
                "channel": "whatsapp",
                "identity": IDENTITY,
                "text": "Aisha Karim",
            },
        ).json()["prompt"]["step"]
        == "consent_cr"
    )
    assert (
        _inbound(attachments=[{"filename": "CR.pdf"}]).json()["prompt"]["step"]
        == "eligibility"
    )
    # Eligibility form arrives as a status_update-style payload (no attachments).
    eligibility = _madad_status("status_update", {"annual_revenue_qar": 1000})
    assert eligibility.status_code == 200
    assert eligibility.json()["prompt"]["step"] == "financials"

    assert (
        _inbound(attachments=[{"filename": "Audited.pdf"}]).json()["prompt"]["step"]
        == "buyers"
    )
    # Buyer info (no attachments — handled as a status_update payload).
    assert (
        _madad_status("status_update", {"name": "ACME"}).json()["prompt"]["step"]
        == "shareholders"
    )
    assert (
        _madad_status(
            "status_update", {"shareholders": [{"name": "A", "percentage": 100}]}
        ).json()["prompt"]["step"]
        == "documents"
    )

    status = client.get("/workflow/status", params={"channel": "whatsapp", "identity": IDENTITY})
    assert status.status_code == 200
    assert status.json()["status"] == "waiting_for_input"

    docs = _inbound(
        attachments=[
            {"filename": "Trade_License.pdf"},
            {"filename": "Tax_Card.pdf"},
            {"filename": "Bank_Statement.pdf"},
        ]
    )
    assert docs.json()["prompt"]["step"] == "journey_wait"

    # Advance the backend status and resume — journey_wait_await → poll →
    # PRE_QUALIFIED → payment_send.
    platform = get_onboarding_platform()
    platform.workflow._identity.journey_status = "PRE_QUALIFIED"  # type: ignore[union-attr]
    assert (
        _madad_status("status_update", {}).json()["prompt"]["step"] == "payment"
    )

    # Payment paid → lender wait.
    assert (
        _madad_status("payment", {"paid": True}).json()["prompt"]["step"]
        == "lender_wait"
    )

    # Backend advances to ACCEPTED → offers → handoff terminal.
    platform.workflow._identity.journey_status = "ACCEPTED"  # type: ignore[union-attr]
    final = _madad_status("status_update", {})
    assert final.json()["completed"] is True
    assert final.json()["outcome"] == "offer_handoff"


def test_unknown_status_event_rejected() -> None:
    response = client.post(
        "/workflow/madad/status/nonsense",
        json={"channel": "whatsapp", "identity": "+97455509999", "payload": {}},
    )
    assert response.status_code == 400
