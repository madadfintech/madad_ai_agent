"""API-level tests for the Nudge service."""

from __future__ import annotations

from fastapi.testclient import TestClient

from app.services.nudge import NudgeScheduleConfig, NudgeStep
from app.services.nudge.deps import get_nudge_service
from app.services.nudge.main import app
from app.shared.workflow.enums import Channel

client = TestClient(app)


def _register_schedule(reason: str) -> None:
    # Configure a schedule on the app's singleton in-memory provider.
    service = get_nudge_service()
    service._configs.add(  # type: ignore[attr-defined]
        NudgeScheduleConfig(
            reason=reason,
            steps=[NudgeStep(offset_seconds=10, channels=[Channel.WHATSAPP], template_key="t")],
        )
    )


def test_health() -> None:
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json()["service"] == "nudge"


def test_start_get_and_suppress_sequence() -> None:
    _register_schedule("api_docs")

    start = client.post(
        "/nudge/sequences",
        json={
            "reason": "api_docs",
            "targets": {"whatsapp": "+97455500050"},
            "target_ref": "APP-API",
        },
    )
    assert start.status_code == 200
    sequence_id = start.json()["sequence_id"]
    assert start.json()["status"] == "active"

    got = client.get(f"/nudge/sequences/{sequence_id}")
    assert got.status_code == 200
    assert len(got.json()["reminders"]) == 1

    suppressed = client.post(f"/nudge/sequences/{sequence_id}/suppress")
    assert suppressed.status_code == 200
    assert suppressed.json()["status"] == "suppressed"


def test_start_unknown_reason_returns_404() -> None:
    response = client.post(
        "/nudge/sequences",
        json={"reason": "no_such_reason", "targets": {"whatsapp": "+97455500051"}},
    )
    assert response.status_code == 404


def test_run_due_endpoint() -> None:
    response = client.post("/nudge/run-due")
    assert response.status_code == 200
    assert "processed" in response.json()
