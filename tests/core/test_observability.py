"""Prometheus /metrics exposure + Sentry init gating."""

from __future__ import annotations

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.core.app import create_service_app
from app.core.config import Settings
from app.core.observability import init_sentry


def _app(**kw) -> FastAPI:
    app = create_service_app(title="Metrics", service="metrics_test", **kw)

    @app.get("/work")
    async def work() -> dict[str, str]:
        return {"ok": "yes"}

    return app


def test_metrics_endpoint_exposes_request_counters():
    client = TestClient(_app())
    client.get("/work")  # generate at least one observation

    resp = client.get("/metrics")
    assert resp.status_code == 200
    assert "text/plain" in resp.headers["content-type"]
    body = resp.text
    assert "http_requests_total" in body
    assert "http_request_duration_seconds" in body
    # The labelled series for our service is present.
    assert 'service="metrics_test"' in body


def test_metrics_disabled_returns_404():
    client = TestClient(_app(metrics=False))
    assert client.get("/metrics").status_code == 404


def test_metrics_endpoint_is_open_on_admin_apps(monkeypatch):
    from app.core.config import settings

    monkeypatch.setattr(settings, "admin_api_token", "secret")
    app = create_service_app(title="Admin", service="admin_metrics", admin=True)
    client = TestClient(app)
    assert client.get("/metrics").status_code == 200  # not gated by admin auth


def test_init_sentry_noop_without_dsn():
    # No DSN configured -> returns False and never imports the SDK.
    assert init_sentry(Settings(), "any") is False
