"""API-level tests for the Operational Visibility service."""

from __future__ import annotations

from fastapi.testclient import TestClient

from app.services.visibility.main import app

client = TestClient(app)


def test_health() -> None:
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json()["service"] == "visibility"


def test_ingest_search_and_dashboard() -> None:
    ingest = client.post(
        "/visibility/activities",
        json={
            "source": "workflow",
            "type": "workflow.run.started",
            "run_id": "r-api-1",
            "workflow": "onboarding",
        },
    )
    assert ingest.status_code == 200

    found = client.get("/visibility/activities", params={"run_id": "r-api-1"})
    assert found.status_code == 200
    assert len(found.json()) == 1

    summary = client.get("/visibility/workflows/r-api-1/summary")
    assert summary.status_code == 200
    assert summary.json()["status"] == "running"

    metrics = client.get("/visibility/metrics")
    assert metrics.status_code == 200
    assert metrics.json()["total_events"] >= 1

    dashboard = client.get("/visibility/dashboard")
    assert dashboard.status_code == 200
    assert "funnel" in dashboard.json()


def test_funnel_and_replay_endpoints() -> None:
    assert client.get("/visibility/funnel").status_code == 200
    # Replay for an unknown conversation is an empty (not failing) timeline.
    replay = client.get("/visibility/conversations/none/replay")
    assert replay.status_code == 200
    assert replay.json()["message_count"] == 0
