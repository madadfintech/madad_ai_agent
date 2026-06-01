"""Shared app factory: probes, correlation id, uniform error mapping, CORS."""

from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.core.app import create_service_app
from app.core.config import settings
from app.core.exceptions import ConflictError, NotFoundError, ValidationError
from app.core.readiness import ReadinessCheck
from app.core.security import UnauthorizedError


def _app(**kw) -> FastAPI:
    app = create_service_app(title="Test", service="test", **kw)

    @app.get("/boom/{code}")
    async def boom(code: str) -> dict[str, str]:
        errors = {
            "404": NotFoundError("missing"),
            "409": ConflictError("conflict"),
            "422": ValidationError("invalid"),
            "401": UnauthorizedError("nope"),
        }
        raise errors[code]

    return app


def test_health_is_liveness_only():
    client = TestClient(_app())
    body = client.get("/health").json()
    assert body == {"status": "ok", "service": "test"}


def test_ready_with_no_checks_is_ready():
    client = TestClient(_app(readiness_checks=[]))
    resp = client.get("/ready")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ready", "checks": []}


def test_ready_reports_failing_check_as_503():
    async def bad() -> None:
        raise RuntimeError("db down")

    checks: list[ReadinessCheck] = [("postgres", bad)]
    client = TestClient(_app(readiness_checks=checks))
    resp = client.get("/ready")
    assert resp.status_code == 503
    body = resp.json()
    assert body["status"] == "not_ready"
    assert "db down" in body["failed"]["postgres"]


def test_correlation_id_generated_and_echoed():
    client = TestClient(_app())
    resp = client.get("/health")
    assert resp.headers.get(settings.request_id_header)  # generated


def test_correlation_id_passed_through():
    client = TestClient(_app())
    resp = client.get("/health", headers={settings.request_id_header: "req-abc"})
    assert resp.headers[settings.request_id_header] == "req-abc"


@pytest.mark.parametrize("code", ["404", "409", "422", "401"])
def test_apperror_status_from_http_status(code: str):
    client = TestClient(_app(), raise_server_exceptions=False)
    resp = client.get(f"/boom/{code}")
    assert resp.status_code == int(code)
    assert resp.json()["message"] in {"missing", "conflict", "invalid", "nope"}


def test_admin_app_blocks_without_token(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(settings, "admin_api_token", "secret")
    app = create_service_app(title="Admin", service="admin", admin=True)

    @app.get("/secret")
    async def secret() -> dict[str, str]:
        return {"ok": "yes"}

    client = TestClient(app)
    # Probes stay open; the protected route is gated.
    assert client.get("/health").status_code == 200
    assert client.get("/ready").status_code == 200
    assert client.get("/secret").status_code == 401
    assert client.get("/secret", headers={"Authorization": "Bearer secret"}).status_code == 200
