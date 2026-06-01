"""Reliability behaviour: retry, retry-exhaustion, timeout, recovery, sweeps."""

from __future__ import annotations

from datetime import timedelta

from app.shared.workflow import Channel, RunStatus, WorkflowEventType

from .workflows import AskNameWorkflow, FlakyWorkflow, SlowWorkflow

WA = Channel.WHATSAPP
IDENTITY = "+97455500002"


async def test_transient_failure_is_retried_then_succeeds(make_runtime):
    runtime = make_runtime(
        retry_max_attempts=3, retry_base_delay_seconds=0.0, retry_jitter=False
    )
    workflow = FlakyWorkflow(fail_times=2)
    runtime.register(workflow)

    result = await runtime.start("test_flaky", WA, IDENTITY)

    assert result.completed
    assert workflow.calls == 3  # two failures + one success
    retried = [e for e in runtime.events.history if e.type == WorkflowEventType.RUN_RETRIED]
    assert len(retried) == 2


async def test_retry_exhaustion_fails_the_run(make_runtime):
    runtime = make_runtime(
        retry_max_attempts=2, retry_base_delay_seconds=0.0, retry_jitter=False
    )
    runtime.register(FlakyWorkflow(fail_times=10))

    result = await runtime.start("test_flaky", WA, IDENTITY)

    assert result.failed
    assert result.status == RunStatus.FAILED
    run = await runtime.run_store.get(result.run.run_id)
    assert run.last_error is not None
    assert WorkflowEventType.RUN_FAILED in [e.type for e in runtime.events.history]


async def test_step_timeout_marks_run_timed_out(make_runtime):
    runtime = make_runtime(step_timeout_seconds=0.05, retry_max_attempts=1)
    runtime.register(SlowWorkflow(sleep_for=1.0))

    result = await runtime.start("test_slow", WA, IDENTITY)

    assert result.status == RunStatus.TIMED_OUT
    assert WorkflowEventType.RUN_TIMED_OUT in [e.type for e in runtime.events.history]


async def test_recover_pending_redrives_interrupted_run(make_runtime):
    runtime = make_runtime()
    runtime.register(AskNameWorkflow())

    result = await runtime.start("test_ask_name", WA, IDENTITY)
    assert result.waiting

    # Simulate a crash mid-flight: the run is left in RUNNING with no process.
    run = await runtime.run_store.get(result.run.run_id)
    run.status = RunStatus.RUNNING
    await runtime.run_store.save(run)

    results = await runtime.recover()

    assert len(results) == 1
    # Re-driving a still-interrupted run lands it back in WAITING_FOR_INPUT.
    assert results[0].status == RunStatus.WAITING_FOR_INPUT
    assert WorkflowEventType.RUN_RECOVERED in [e.type for e in runtime.events.history]


async def test_timeout_sweep_expires_lapsed_sessions(make_runtime):
    runtime = make_runtime(session_ttl_seconds=3600)
    runtime.register(AskNameWorkflow())

    result = await runtime.start("test_ask_name", WA, IDENTITY)
    run = await runtime.run_store.get(result.run.run_id)
    assert run.expires_at is not None

    future = run.expires_at + timedelta(seconds=1)
    expired = await runtime.recovery.sweep_timeouts(now=future)

    assert len(expired) == 1
    refreshed = await runtime.run_store.get(result.run.run_id)
    assert refreshed.status == RunStatus.TIMED_OUT

    session = await runtime.sessions.get(WA, IDENTITY)
    assert session.status.value == "expired"

    event_types = [e.type for e in runtime.events.history]
    assert WorkflowEventType.RUN_TIMED_OUT in event_types
    assert WorkflowEventType.SESSION_EXPIRED in event_types
