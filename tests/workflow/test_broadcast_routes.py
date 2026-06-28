"""HTTP integration tests for the bulk-broadcast endpoints.

These exercise the FastAPI surface end-to-end with the in-memory
broadcast store + a stub start callable injected via dependency
override. The BackgroundTasks runner in TestClient runs synchronously
on the request thread, so by the time the response is returned the
coordinator has fully drained the batch — convenient for assertions.
"""

from __future__ import annotations

from typing import Any

import pytest
from fastapi.testclient import TestClient

from app.services.workflow import main as workflow_main
from app.services.workflow.broadcast import (
    InMemoryBroadcastStore,
)

# ---------------------------------------------------------------------------
# Test client with overrides
# ---------------------------------------------------------------------------


class _StubDedupe:
    async def claim(self, key: str, *, ttl_seconds: int) -> bool:
        return True   # always claim — no dedup interference in this test


class _StubDispatcher:
    _dedupe = _StubDedupe()


class _StubRunStore:
    async def list_by_status(self, *statuses):
        return []


class _StubRuntime:
    run_store = _StubRunStore()
    async def start(self, name, channel, identity, **kw):
        return {"run_id": f"run_{identity}", "channel": str(channel)}


class _StubPlatform:
    """Just enough surface to satisfy the route's Depends + the inner
    _start_one_for_broadcast helper."""

    dispatcher = _StubDispatcher()
    runtime = _StubRuntime()


@pytest.fixture
def client(monkeypatch):
    # Use an in-memory store so each test gets a fresh state.
    store = InMemoryBroadcastStore()

    def _stub_store():
        return store

    def _stub_platform():
        return _StubPlatform()

    # Lock both deps in place for the request.
    workflow_main.app.dependency_overrides[
        workflow_main.get_broadcast_store
    ] = _stub_store
    workflow_main.app.dependency_overrides[
        workflow_main.get_onboarding_platform
    ] = _stub_platform
    yield TestClient(workflow_main.app), store
    workflow_main.app.dependency_overrides.clear()


# ---------------------------------------------------------------------------
# Submit
# ---------------------------------------------------------------------------


def _post_submit(client, csv_text: str, **fields) -> Any:
    files = {"file": ("upload.csv", csv_text, "text/csv")}
    data = {
        "channel": "whatsapp",
        "idempotency_key": "test-key-1",
        "dry_run": "true",
        "rate_per_minute": "60",
        **fields,
    }
    return client.post("/workflow/campaign/broadcast", files=files, data=data)


class TestSubmit:
    def test_happy_path_dry_run(self, client) -> None:
        c, store = client
        r = _post_submit(c,
            "phone,locale,name\n"
            "+97455500001,en,Sara\n"
            "+97455500002,en,Ali\n"
        )
        assert r.status_code == 202, r.text
        body = r.json()
        assert body["total_rows"] == 2
        assert body["valid_rows"] == 2
        assert body["invalid_rows"] == 0
        assert body["deduped"] is False
        # Background task finished synchronously in TestClient.
        batch = list(store._batches.values())[0]  # type: ignore[attr-defined]
        assert batch.status == "completed"
        assert batch.sent == 2

    def test_missing_phone_column_400(self, client) -> None:
        c, _ = client
        r = _post_submit(c, "name\nSara\n")
        assert r.status_code == 400

    def test_invalid_rows_reported_but_batch_proceeds(self, client) -> None:
        c, store = client
        r = _post_submit(c,
            "phone\n"
            "+97455500001\n"
            "not-a-number\n"
            "+97455500003\n"
        )
        assert r.status_code == 202
        body = r.json()
        assert body["total_rows"] == 3
        assert body["valid_rows"] == 2
        assert body["invalid_rows"] == 1
        assert body["invalid_details"][0]["raw_phone"] == "not-a-number"
        batch = list(store._batches.values())[0]  # type: ignore[attr-defined]
        assert batch.sent == 2

    def test_idempotency_returns_same_batch_id(self, client) -> None:
        c, store = client
        r1 = _post_submit(c, "phone\n+97455500001\n", idempotency_key="dedupe-key")
        r2 = _post_submit(c, "phone\n+97455500002\n", idempotency_key="dedupe-key")
        assert r1.status_code == 202
        assert r2.status_code == 202
        assert r1.json()["batch_id"] == r2.json()["batch_id"]
        assert r2.json()["deduped"] is True
        # Only one batch persisted.
        assert len(store._batches) == 1  # type: ignore[attr-defined]


# ---------------------------------------------------------------------------
# Get + list
# ---------------------------------------------------------------------------


class TestGet:
    def test_get_existing(self, client) -> None:
        c, store = client
        r1 = _post_submit(c, "phone\n+97455500001\n")
        bid = r1.json()["batch_id"]
        r2 = c.get(f"/workflow/campaign/broadcast/{bid}")
        assert r2.status_code == 200
        body = r2.json()
        assert body["batch_id"] == bid
        assert body["channel"] == "whatsapp"

    def test_get_404(self, client) -> None:
        c, _ = client
        r = c.get("/workflow/campaign/broadcast/bcast_does_not_exist")
        assert r.status_code == 404


class TestList:
    def test_list_empty(self, client) -> None:
        c, _ = client
        r = c.get("/workflow/campaign/broadcast")
        assert r.status_code == 200
        assert r.json() == []

    def test_list_newest_first(self, client) -> None:
        c, _ = client
        _post_submit(c, "phone\n+97455500001\n", idempotency_key="batch-001")
        _post_submit(c, "phone\n+97455500002\n", idempotency_key="batch-002")
        r = c.get("/workflow/campaign/broadcast")
        assert r.status_code == 200
        body = r.json()
        assert len(body) == 2
        # Newest first.
        assert body[0]["idempotency_key"] == "batch-002"
        assert body[1]["idempotency_key"] == "batch-001"
