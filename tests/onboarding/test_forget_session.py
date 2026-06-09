"""Admin reset endpoint (Ishan handover §10, 2026-06-09).

QA cannot re-test from a clean state because the agent owns three stores
(workflow.runs + LangGraph checkpoints + Redis session). The forget-session
endpoint clears all three for one identity in one call so the SME's next
inbound is treated as a brand-new conversation.
"""

from __future__ import annotations

import pytest
from httpx import ASGITransport, AsyncClient

from app.services.workflow.main import app
from app.shared.workflow import Channel
from app.shared.workflow.utils import derive_session_id

WA = Channel.WHATSAPP
IDENTITY = "+97455500077"


@pytest.fixture(autouse=True)
def _clear_singleton():
    from app.services.workflow.deps import get_onboarding_platform

    get_onboarding_platform.cache_clear()
    yield
    get_onboarding_platform.cache_clear()


async def test_forget_session_clears_runs_and_session() -> None:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        # Park a run for this identity so there is state to clear.
        start = await client.post(
            "/workflow/campaign/start",
            json={"channel": "whatsapp", "identity": IDENTITY, "locale": "en"},
        )
        assert start.status_code == 200, start.text

        # Fire the admin reset.
        forget = await client.post(
            "/workflow/admin/forget-session",
            json={"channel": "whatsapp", "identity": IDENTITY},
        )
        assert forget.status_code == 200, forget.text
        body = forget.json()
        assert body["session_cleared"] is True
        assert body["deleted_runs"] >= 1
        assert isinstance(body["thread_ids"], list)
        assert body["thread_ids"]

        # Status should now report no active session.
        status = await client.get(
            "/workflow/status",
            params={"channel": "whatsapp", "identity": IDENTITY},
        )
        # Either 404 (session missing) OR a 4xx; the post-reset state must
        # NOT report an active run.
        assert status.status_code in {404, 400}


async def test_forget_session_is_idempotent_on_empty_state() -> None:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        forget = await client.post(
            "/workflow/admin/forget-session",
            json={"channel": "whatsapp", "identity": "+97455500099"},
        )
        assert forget.status_code == 200
        body = forget.json()
        assert body["session_cleared"] is False
        assert body["deleted_runs"] == 0
        assert body["deleted_checkpoint_threads"] == 0
        assert body["thread_ids"] == []


def test_derive_session_id_is_deterministic() -> None:
    # The reset must target the exact same key the dispatcher writes,
    # otherwise a successful 200 leaves real state behind.
    a = derive_session_id(WA, IDENTITY)
    b = derive_session_id(WA, IDENTITY)
    assert a == b
