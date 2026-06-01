"""PostgreSQL workflow run-store adapter (verified on SQLite)."""

from __future__ import annotations

from datetime import timedelta

import pytest

from app.shared.workflow.adapters.postgres_runstore import PostgresWorkflowRunStore
from app.shared.workflow.enums import RunStatus
from app.shared.workflow.errors import RunNotFoundError
from app.shared.workflow.persistence import AuditEntry, WorkflowRun
from app.shared.workflow.utils import utcnow


def _run(**kw) -> WorkflowRun:
    return WorkflowRun(workflow="onboarding", session_id="s1", thread_id="t1", **kw)


async def test_create_get_save_roundtrip(db):
    store = PostgresWorkflowRunStore(db)
    run = _run(status=RunStatus.RUNNING)
    await store.create(run)

    fetched = await store.get(run.run_id)
    assert fetched.run_id == run.run_id
    assert fetched.status == RunStatus.RUNNING
    assert fetched.workflow == "onboarding"

    run.status = RunStatus.COMPLETED
    await store.save(run)
    assert (await store.get(run.run_id)).status == RunStatus.COMPLETED


async def test_get_missing_raises(db):
    store = PostgresWorkflowRunStore(db)
    assert await store.get_or_none("run_missing") is None
    with pytest.raises(RunNotFoundError):
        await store.get("run_missing")


async def test_list_by_status_and_recoverable(db):
    store = PostgresWorkflowRunStore(db)
    await store.create(_run(status=RunStatus.RUNNING))
    await store.create(_run(status=RunStatus.SUSPENDED))
    await store.create(_run(status=RunStatus.COMPLETED))

    running = await store.list_by_status(RunStatus.RUNNING)
    assert len(running) == 1

    recoverable = await store.list_recoverable()
    assert {r.status for r in recoverable} == {RunStatus.RUNNING, RunStatus.SUSPENDED}


async def test_list_waiting_expired(db):
    store = PostgresWorkflowRunStore(db)
    now = utcnow()
    await store.create(
        _run(status=RunStatus.WAITING_FOR_INPUT, expires_at=now - timedelta(minutes=1))
    )
    await store.create(
        _run(status=RunStatus.WAITING_FOR_INPUT, expires_at=now + timedelta(hours=1))
    )

    expired = await store.list_waiting_expired(now)
    assert len(expired) == 1


async def test_audit_trail(db):
    store = PostgresWorkflowRunStore(db)
    run = _run(status=RunStatus.RUNNING)
    await store.create(run)
    await store.append_audit(AuditEntry(run_id=run.run_id, action="start"))
    await store.append_audit(AuditEntry(run_id=run.run_id, action="complete"))

    entries = await store.list_audit(run.run_id)
    assert [e.action for e in entries] == ["start", "complete"]
