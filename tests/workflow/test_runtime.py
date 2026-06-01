"""End-to-end runtime behaviour: completion, interrupt/resume, reconnect."""

from __future__ import annotations

import pytest

from app.shared.workflow import (
    Channel,
    RunStatus,
    SessionStatus,
    WorkflowEventType,
)
from app.shared.workflow.errors import SessionNotFoundError, WorkflowNotFoundError

from .workflows import AskNameWorkflow, LinearWorkflow

WA = Channel.WHATSAPP
IDENTITY = "+97455500001"


async def test_linear_workflow_runs_to_completion(runtime):
    runtime.register(LinearWorkflow())

    result = await runtime.start("test_linear", WA, IDENTITY)

    assert result.completed
    assert result.status == RunStatus.COMPLETED
    assert result.values["greeted"] is True
    assert result.values["finished"] is True

    run = await runtime.run_store.get(result.run.run_id)
    assert run.status == RunStatus.COMPLETED
    assert run.completed_at is not None

    session = await runtime.sessions.get(WA, IDENTITY)
    assert session is not None
    assert session.status == SessionStatus.COMPLETED

    event_types = [e.type for e in runtime.events.history]
    assert WorkflowEventType.RUN_STARTED in event_types
    assert WorkflowEventType.RUN_COMPLETED in event_types


async def test_interrupt_then_resume(runtime):
    runtime.register(AskNameWorkflow())

    result = await runtime.start("test_ask_name", WA, IDENTITY)

    assert result.waiting
    assert result.status == RunStatus.WAITING_FOR_INPUT
    assert result.prompt == {"prompt": "What is your name?"}

    session = await runtime.sessions.get(WA, IDENTITY)
    assert session.status == SessionStatus.WAITING
    assert session.active_run_id == result.run.run_id

    resumed = await runtime.resume(WA, IDENTITY, message="Jathish")

    assert resumed.completed
    assert resumed.values["name"] == "Jathish"
    assert resumed.values["data"]["greeting"] == "Hello Jathish"

    session = await runtime.sessions.get(WA, IDENTITY)
    assert session.status == SessionStatus.COMPLETED


async def test_reconnect_resumes_same_run(runtime):
    runtime.register(AskNameWorkflow())

    first = await runtime.start("test_ask_name", WA, IDENTITY)
    # A later inbound message (reconnect) resolves the same session + run.
    second = await runtime.resume(WA, IDENTITY, message="Sam")

    assert second.run.run_id == first.run.run_id
    assert second.completed


async def test_resume_without_active_session_raises(runtime):
    runtime.register(AskNameWorkflow())
    with pytest.raises(SessionNotFoundError):
        await runtime.resume(WA, "+97455509999", message="hello")


async def test_unknown_workflow_raises(runtime):
    with pytest.raises(WorkflowNotFoundError):
        await runtime.start("does_not_exist", WA, IDENTITY)


async def test_email_channel_independent_session(runtime):
    runtime.register(AskNameWorkflow())

    wa = await runtime.start("test_ask_name", Channel.WHATSAPP, "+97455500777")
    email = await runtime.start("test_ask_name", Channel.EMAIL, "sme@example.com")

    # Same identity string would still differ by channel; here they're distinct.
    assert wa.run.session_id != email.run.session_id
    assert wa.waiting and email.waiting
