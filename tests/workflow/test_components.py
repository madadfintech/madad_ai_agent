"""Unit tests for individual runtime components."""

from __future__ import annotations

import pytest

from app.shared.workflow import (
    AuditLogger,
    Channel,
    InMemoryEventBus,
    InMemoryWorkflowRunStore,
    RunStatus,
    TransitionManager,
    WorkflowEvent,
    WorkflowEventType,
    WorkflowRegistry,
    WorkflowRun,
    derive_session_id,
)
from app.shared.workflow.errors import (
    InvalidTransitionError,
    WorkflowAlreadyRegisteredError,
    WorkflowNotFoundError,
)
from app.shared.workflow.utils import compute_backoff

from .workflows import LinearWorkflow


def test_registry_register_and_get():
    registry = WorkflowRegistry()
    workflow = LinearWorkflow()
    registry.register(workflow)

    assert registry.get("test_linear") is workflow
    assert registry.get("test_linear", 1) is workflow
    assert registry.exists("test_linear")
    assert ("test_linear", 1) in registry.list_workflows()


def test_registry_duplicate_raises():
    registry = WorkflowRegistry()
    registry.register(LinearWorkflow())
    with pytest.raises(WorkflowAlreadyRegisteredError):
        registry.register(LinearWorkflow())


def test_registry_missing_raises():
    registry = WorkflowRegistry()
    with pytest.raises(WorkflowNotFoundError):
        registry.get("nope")


def test_derive_session_id_is_stable_and_channel_scoped():
    a = derive_session_id(Channel.WHATSAPP, "+97455500000")
    b = derive_session_id(Channel.WHATSAPP, "+97455500000")
    assert a == b
    assert a.startswith("sess_whatsapp_")
    assert derive_session_id(Channel.EMAIL, "x@y.com") != a


def test_compute_backoff_doubles_and_caps():
    assert compute_backoff(1, base_delay=1, max_delay=10, jitter=False) == 1
    assert compute_backoff(2, base_delay=1, max_delay=10, jitter=False) == 2
    assert compute_backoff(3, base_delay=1, max_delay=10, jitter=False) == 4
    assert compute_backoff(5, base_delay=1, max_delay=10, jitter=False) == 10


async def test_transition_guard_rejects_illegal_moves():
    store = InMemoryWorkflowRunStore()
    transitions = TransitionManager(store, AuditLogger(store))
    run = WorkflowRun(workflow="w", session_id="s", thread_id="t")
    await store.create(run)

    await transitions.transition(run, RunStatus.RUNNING)
    await transitions.transition(run, RunStatus.COMPLETED)

    assert run.status == RunStatus.COMPLETED
    with pytest.raises(InvalidTransitionError):
        await transitions.transition(run, RunStatus.RUNNING)  # terminal -> running


async def test_event_bus_fans_out_to_subscribers():
    bus = InMemoryEventBus()
    received: list[WorkflowEvent] = []

    async def handler(event: WorkflowEvent) -> None:
        received.append(event)

    bus.subscribe(handler)
    await bus.publish(
        WorkflowEvent(
            type=WorkflowEventType.RUN_STARTED,
            run_id="r1",
            session_id="s1",
            workflow="w",
        )
    )

    assert len(received) == 1
    assert received[0].run_id == "r1"
    assert len(bus.history) == 1


async def test_audit_logger_persists_entries():
    store = InMemoryWorkflowRunStore()
    audit = AuditLogger(store)
    await audit.record("run1", "custom_action", detail={"x": 1})

    entries = await store.list_audit("run1")
    assert len(entries) == 1
    assert entries[0].action == "custom_action"
    assert entries[0].detail == {"x": 1}
