"""API-level tests for the CMS admin endpoints."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.core.config import settings
from app.services.cms.main import app

client = TestClient(app)


def test_health() -> None:
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json()["service"] == "cms"


def test_admin_auth_enforced_when_token_set(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "admin_api_token", "secret-token")

    # Health stays open.
    assert client.get("/health").status_code == 200
    # Admin route without the bearer token is rejected.
    assert client.get("/cms/templates/x", params={"locale": "en"}).status_code == 401
    # With the token it passes auth (404 because the template doesn't exist).
    ok = client.get(
        "/cms/templates/x",
        params={"locale": "en"},
        headers={"Authorization": "Bearer secret-token"},
    )
    assert ok.status_code == 404


def test_template_upsert_get_and_versions() -> None:
    client.post(
        "/cms/templates",
        json={"name": "consent", "locale": "en", "body": "Please share your CR."},
    )
    updated = client.post(
        "/cms/templates",
        json={"name": "consent", "locale": "en", "body": "Kindly share your CR."},
    )
    assert updated.status_code == 200
    assert updated.json()["version"] == 2

    got = client.get("/cms/templates/consent", params={"locale": "en"})
    assert got.status_code == 200
    assert got.json()["value"]["body"] == "Kindly share your CR."

    versions = client.get(
        "/cms/configs/template/consent/versions", params={"locale": "en"}
    )
    assert versions.status_code == 200
    assert [v["version"] for v in versions.json()] == [1, 2]


def test_rollback_via_api() -> None:
    client.post("/cms/templates", json={"name": "fee", "locale": "en", "body": "QAR 6,000"})
    client.post("/cms/templates", json={"name": "fee", "locale": "en", "body": "QAR 7,000"})

    rolled = client.post(
        "/cms/configs/template/fee/rollback",
        json={"target_version": 1, "locale": "en"},
    )
    assert rolled.status_code == 200
    assert rolled.json()["value"]["body"] == "QAR 6,000"


def test_checklist_add_document_reflects() -> None:
    client.post(
        "/cms/checklists/onboarding",
        json={"items": [{"code": "trade_license", "label": {"en": "Trade License"}}]},
    )
    client.post(
        "/cms/checklists/onboarding",
        json={
            "items": [
                {"code": "trade_license", "label": {"en": "Trade License"}},
                {"code": "tax_card", "label": {"en": "Tax Card"}},
            ]
        },
    )
    got = client.get("/cms/checklists/onboarding")
    assert got.status_code == 200
    assert [i["code"] for i in got.json()["items"]] == ["trade_license", "tax_card"]


def test_variables_round_trip() -> None:
    client.post("/cms/variables", json={"variables": {"phone": "72773652"}})
    got = client.get("/cms/variables")
    assert got.status_code == 200
    assert got.json()["phone"] == "72773652"


def test_invalid_template_returns_422() -> None:
    response = client.post(
        "/cms/templates", json={"name": "bad", "locale": "en", "body": "  "}
    )
    assert response.status_code == 422


def test_missing_template_returns_404() -> None:
    response = client.get("/cms/templates/does_not_exist", params={"locale": "en"})
    assert response.status_code == 404
