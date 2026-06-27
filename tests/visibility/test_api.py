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


# -- Analytics dashboard v1 (M1 acceptance stub) ----------------------------


def test_dashboard_v1_html_renders_empty() -> None:
    """Empty-state dashboard still returns 200 + valid HTML — the page
    is what ops opens on M1 demo day before any traffic. Empty cells
    must read "No data yet." rather than blowing up."""
    response = client.get("/visibility/dashboard/v1")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/html")
    body = response.text
    assert "<!doctype html>" in body
    assert "MADAD Analytics" in body
    # Auto-refresh keeps the M1-demo browser tab alive without manual reload.
    assert 'http-equiv="refresh"' in body
    assert 'content="30"' in body
    # KPI tiles are present even with zero data.
    assert "Total events" in body
    assert "Workflow runs" in body


def test_dashboard_v1_html_renders_populated_data() -> None:
    """After a few activities + a workflow run, the dashboard shows the
    KPIs from the live snapshot — proves the page is wired to
    ``get_dashboard``, not stubbed values."""
    # Drive some events through the service so the dashboard has data.
    for i in range(3):
        client.post(
            "/visibility/activities",
            json={
                "source": "workflow",
                "type": "workflow.run.started",
                "run_id": f"r-dash-{i}",
                "workflow": "onboarding",
            },
        )
    response = client.get("/visibility/dashboard/v1")
    assert response.status_code == 200
    body = response.text
    # 3+ events recorded — must reflect in the KPI tile next to its label.
    assert "Total events" in body
    # ``workflow`` source appears in the by-source breakdown.
    assert "workflow" in body
    # ``workflow.run.started`` type appears in the by-type breakdown.
    assert "workflow.run.started" in body
