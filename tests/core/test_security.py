"""JWT API auth + webhook HMAC verification (both dev-open until configured)."""

from __future__ import annotations

import hashlib
import hmac
import time

import jwt
import pytest
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient

from app.core.app import create_service_app
from app.core.config import settings
from app.core.security import verify_webhook_signature


def _api_app() -> FastAPI:
    app = create_service_app(title="API", service="api_test", api_auth=True)

    @app.get("/protected")
    async def protected() -> dict[str, str]:
        return {"ok": "yes"}

    @app.post("/svc/webhooks/ping")
    async def hook() -> dict[str, str]:
        return {"hook": "ok"}

    return app


def _token(secret: str, *, exp_offset: int = 60, **claims) -> str:
    payload = {"sub": "svc", "exp": int(time.time()) + exp_offset, **claims}
    return jwt.encode(payload, secret, algorithm="HS256")


# -- JWT api auth ------------------------------------------------------------


def test_api_auth_dev_open_without_secret():
    client = TestClient(_api_app())
    assert client.get("/protected").status_code == 200


def test_api_auth_rejects_missing_and_invalid_token(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(settings.security, "jwt_secret", "topsecret-key-padded-to-32-bytes-x")
    client = TestClient(_api_app())

    assert client.get("/protected").status_code == 401  # no token
    assert client.get(
        "/protected", headers={"Authorization": "Bearer not-a-jwt"}
    ).status_code == 401  # malformed
    assert client.get(
        "/protected",
        headers={"Authorization": f"Bearer {_token('wrong-secret-padded-to-32-bytes-x')}"},
    ).status_code == 401  # bad signature


def test_api_auth_rejects_expired_token(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(settings.security, "jwt_secret", "topsecret-key-padded-to-32-bytes-x")
    client = TestClient(_api_app())
    expired = _token("topsecret-key-padded-to-32-bytes-x", exp_offset=-10)
    assert client.get(
        "/protected", headers={"Authorization": f"Bearer {expired}"}
    ).status_code == 401


def test_api_auth_accepts_valid_token(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(settings.security, "jwt_secret", "topsecret-key-padded-to-32-bytes-x")
    client = TestClient(_api_app())
    ok = _token("topsecret-key-padded-to-32-bytes-x")
    assert client.get(
        "/protected", headers={"Authorization": f"Bearer {ok}"}
    ).status_code == 200


def test_api_auth_validates_audience_when_configured(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(settings.security, "jwt_secret", "topsecret-key-padded-to-32-bytes-x")
    monkeypatch.setattr(settings.security, "jwt_audience", "madad-api")
    client = TestClient(_api_app())

    wrong_aud = _token("topsecret-key-padded-to-32-bytes-x", aud="someone-else")
    assert client.get(
        "/protected", headers={"Authorization": f"Bearer {wrong_aud}"}
    ).status_code == 401
    right_aud = _token("topsecret-key-padded-to-32-bytes-x", aud="madad-api")
    assert client.get(
        "/protected", headers={"Authorization": f"Bearer {right_aud}"}
    ).status_code == 200


def test_probes_and_webhooks_exempt_from_jwt(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(settings.security, "jwt_secret", "topsecret-key-padded-to-32-bytes-x")
    client = TestClient(_api_app())
    assert client.get("/health").status_code == 200  # probe exempt
    assert client.post("/svc/webhooks/ping").status_code == 200  # webhook exempt (uses HMAC)


# -- webhook HMAC signature --------------------------------------------------


def _hook_app() -> FastAPI:
    app = create_service_app(title="Hook", service="hook_test")

    @app.post("/hook", dependencies=[Depends(verify_webhook_signature)])
    async def hook() -> dict[str, str]:
        return {"received": "ok"}

    return app


def _sign(secret: str, body: bytes) -> str:
    return hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


def test_webhook_dev_open_without_secret():
    client = TestClient(_hook_app())
    assert client.post("/hook", content=b"{}").status_code == 200


def test_webhook_rejects_missing_and_wrong_signature(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(settings.security, "webhook_secret", "wh-secret")
    client = TestClient(_hook_app())
    body = b'{"event":"offer"}'

    assert client.post("/hook", content=body).status_code == 401  # no signature
    assert client.post(
        "/hook", content=body, headers={"X-Madad-Signature": "deadbeef"}
    ).status_code == 401  # wrong signature


def test_webhook_accepts_valid_signature(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(settings.security, "webhook_secret", "wh-secret")
    client = TestClient(_hook_app())
    body = b'{"event":"offer"}'
    sig = _sign("wh-secret", body)
    resp = client.post("/hook", content=body, headers={"X-Madad-Signature": sig})
    assert resp.status_code == 200
    assert resp.json() == {"received": "ok"}
