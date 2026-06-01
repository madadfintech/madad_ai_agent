"""API-level tests for the Document Intelligence service."""

from __future__ import annotations

from fastapi.testclient import TestClient

from app.services.document.main import app

from .conftest import make_zip

client = TestClient(app)


def test_health() -> None:
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json()["service"] == "document"


def test_upload_document() -> None:
    response = client.post(
        "/documents/upload",
        files={"file": ("cr.pdf", b"%PDF-data", "application/pdf")},
        data={"application_ref": "APP-API", "kind": "onboarding"},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "completed"
    assert body["filename"] == "cr.pdf"


def test_upload_zip_creates_batch() -> None:
    content = make_zip({"a.pdf": b"a", "b.pdf": b"b"})
    response = client.post(
        "/documents/zip",
        files={"file": ("docs.zip", content, "application/zip")},
        data={"application_ref": "APP-ZIP", "kind": "onboarding"},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["document_count"] == 2
    assert len(body["documents"]) == 2


def test_unknown_document_returns_404() -> None:
    response = client.get("/documents/doc_does_not_exist")
    assert response.status_code == 404
