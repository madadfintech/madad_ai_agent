"""Workflow service admin-ops API — surfaced to MADAD backend.

* ``GET /workflow/admin/ops/runs``       — paginated workflow runs.
* ``GET /workflow/admin/ops/runs/{id}``  — full run detail + scrubbed state.
* ``GET /workflow/admin/ops/summary``    — high-level counts + Phase 1.b totals.

All read-only; reset stays on the existing ``/workflow/admin/forget-session``.
"""

from __future__ import annotations

from fastapi.testclient import TestClient

from app.services.workflow.deps import get_onboarding_platform
from app.services.workflow.main import app, reset_ops_summary_cache

client = TestClient(app)
DOC = "ZHVtbXk="


def _start_minimal_runs(n: int) -> list[str]:
    """Start ``n`` independent runs that each park at the campaign prompt
    so the ops list has something to list."""
    identities = []
    for i in range(n):
        identity = f"+97455500A0{i}"
        client.post(
            "/workflow/campaign/start",
            json={"channel": "whatsapp", "identity": identity},
        )
        identities.append(identity)
    return identities


def test_ops_list_runs_returns_known_runs() -> None:
    """Each campaign/start lands in the store; the ops list must surface
    them with status + step + identity."""
    identities = _start_minimal_runs(2)
    response = client.get("/workflow/admin/ops/runs")

    assert response.status_code == 200
    body = response.json()
    assert "runs" in body
    assert body["total"] >= 2
    rows = {r["identity"]: r for r in body["runs"] if r["identity"] in identities}
    assert set(rows.keys()) == set(identities)
    for r in rows.values():
        assert r["status"] == "waiting_for_input"
        assert r["current_step"] == "campaign_await"
        assert r["workflow"] == "onboarding"


def test_ops_list_runs_pagination_and_filter() -> None:
    _start_minimal_runs(3)
    response = client.get(
        "/workflow/admin/ops/runs?status=waiting_for_input&limit=1&offset=0"
    )
    assert response.status_code == 200
    body = response.json()
    assert len(body["runs"]) == 1
    assert body["limit"] == 1
    assert body["offset"] == 0
    # Total reflects filtered count, not just the page.
    assert body["total"] >= 1


def test_ops_run_detail_returns_state_and_pending_interrupts() -> None:
    identities = _start_minimal_runs(1)
    identity = identities[0]
    # Find the run id from the list.
    list_resp = client.get(
        "/workflow/admin/ops/runs?status=waiting_for_input"
    )
    matching = [
        r for r in list_resp.json()["runs"] if r["identity"] == identity
    ]
    assert matching, "must surface the start"
    run_id = matching[0]["run_id"]

    response = client.get(f"/workflow/admin/ops/runs/{run_id}")
    assert response.status_code == 200
    detail = response.json()
    assert detail["run_id"] == run_id
    assert detail["identity"] == identity
    assert detail["status"] == "waiting_for_input"
    # Pending interrupt set so the operator can see what we're waiting on.
    assert detail["pending_interrupts"], detail
    # Secret tokens stripped.
    assert "access_token" not in detail["state"]
    assert "refresh_token" not in detail["state"]


def test_ops_run_detail_404_for_unknown_id() -> None:
    response = client.get("/workflow/admin/ops/runs/run_does_not_exist")
    # SessionNotFoundError maps to a 404 via AppError.
    assert response.status_code in (404, 410)


def test_ops_summary_aggregates_phase1b_totals() -> None:
    _start_minimal_runs(2)
    response = client.get("/workflow/admin/ops/summary")
    assert response.status_code == 200
    summary = response.json()
    assert summary["runs_total"] >= 2
    assert summary["runs_waiting"] >= 2
    # Phase 1.b counters default to 0 for fresh runs.
    assert summary["invoices_submitted"] >= 0
    assert summary["disbursements_received"] >= 0


async def test_ops_summary_phase1b_counts_after_invoice_submission() -> None:
    """Drive a run programmatically through to invoice_collect_await,
    submit one invoice, then summary should reflect invoices_submitted >= 1.

    Driven via the platform directly so the dispatcher chokepoint's
    dedup + channel-identity-resolution don't muddy the test.
    """
    from app.shared.workflow import Channel

    identity = "+97455500A99"
    wa = Channel.WHATSAPP
    platform = get_onboarding_platform()
    runtime = platform.runtime

    async def resume(message):
        return await runtime.resume(wa, identity, message=message)

    await runtime.start("onboarding", wa, identity, input={"trigger": "campaign"})
    await resume({"text": "YES"})
    await resume({"text": "biz@example.com"})
    await resume({"attachments": [{"filename": "CR.pdf", "content_base64": DOC}]})
    await resume({"attachments": [{"filename": "Audited.pdf", "content_base64": DOC}]})
    await resume({"event": "prequalification.completed", "madadScore": 78})
    await resume(
        {"attachments": [{"filename": "Establishment.pdf", "content_base64": DOC}]}
    )
    platform.workflow._identity.journey_status = "QUALIFIED"  # type: ignore[union-attr]
    await resume({"event": "madad_score.ready", "journey_status": "QUALIFIED"})
    await resume({"type": "payment", "paid": True})
    for status in ("ACCEPTED", "OFFER_ACCEPTED", "ACTIVATED"):
        platform.workflow._identity.journey_status = status  # type: ignore[union-attr]
        await resume({"type": "status_update"})

    # Now at invoice_collect_await — submit one invoice.
    await resume({"attachments": [{"filename": "INV-1.pdf", "content_base64": DOC}]})

    # /ops/summary is cached for 30s in prod; for this test we invalidate
    # so we read the post-submission counts directly.
    reset_ops_summary_cache()
    summary = client.get("/workflow/admin/ops/summary").json()
    assert summary["invoices_submitted"] >= 1
