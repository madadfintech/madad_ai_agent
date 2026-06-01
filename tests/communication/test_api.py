"""API-level tests for the Communication Service FastAPI app."""

from __future__ import annotations

from fastapi.testclient import TestClient

from app.services.communication.main import app

client = TestClient(app)


def test_health() -> None:
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json()["service"] == "communication"


def test_inbound_outbound_delivery_flow() -> None:
    identity = "+97455500099"

    inbound = client.post(
        "/comm/inbound",
        json={"channel": "whatsapp", "identity": identity, "text": "YES"},
    )
    assert inbound.status_code == 200
    inbound_body = inbound.json()
    assert inbound_body["direction"] == "inbound"
    conversation_id = inbound_body["conversation_id"]

    outbound = client.post(
        "/comm/outbound",
        json={"channel": "whatsapp", "identity": identity, "text": "Hello!"},
    )
    assert outbound.status_code == 200
    outbound_body = outbound.json()
    assert outbound_body["status"] == "sent"
    assert outbound_body["conversation_id"] == conversation_id
    provider_id = outbound_body["provider_message_id"]
    assert provider_id

    delivery = client.post(
        "/comm/delivery",
        json={"provider_message_id": provider_id, "status": "delivered"},
    )
    assert delivery.status_code == 200
    assert delivery.json()["status"] == "delivered"

    history = client.get(f"/comm/conversations/{conversation_id}/messages")
    assert history.status_code == 200
    assert len(history.json()) == 2


def test_outbound_requires_exactly_one_body() -> None:
    response = client.post(
        "/comm/outbound",
        json={"channel": "whatsapp", "identity": "+97455500098", "text": "x", "template_key": "y"},
    )
    assert response.status_code == 422
