"""QA #2 (2026-06-09): transient backend errors used to leave a run in
status=FAILED permanently. The next inbound message hit the dispatcher's
TERMINAL_STATUSES branch, which started a fresh run — the SME effectively
had to redo everything they'd already completed.

The dispatcher now distinguishes between "intentional terminal" (COMPLETED,
CANCELLED, DEAD_LETTERED — leave alone) and "transient terminal" (FAILED,
TIMED_OUT — revive). On a revivable status, the run is transitioned back
to RUNNING and the new inbound is replayed at the last checkpoint, so the
SME picks up where they left off.
"""

from __future__ import annotations

from app.services.workflow.dispatcher import (
    REVIVABLE_TERMINAL_STATUSES,
    OnboardingDispatcher,
)
from app.shared.workflow import Channel, RunStatus

WA = Channel.WHATSAPP
IDENTITY = "+97455500201"


async def test_failed_run_is_revived_on_next_inbound(harness) -> None:
    runtime = harness.platform.runtime
    dispatcher = OnboardingDispatcher(runtime)

    # Drive to a known parked step so there's a real checkpoint to revive into.
    await runtime.start("onboarding", WA, IDENTITY, input={"trigger": "campaign"})
    await runtime.resume(WA, IDENTITY, message={"text": "YES"})

    # Look up the run, simulate a transient backend failure that left the
    # run terminally FAILED — exactly what the QA report described.
    session = await runtime.sessions.get(WA, IDENTITY)
    assert session is not None and session.active_run_id
    run = await runtime.run_store.get(session.active_run_id)
    run.status = RunStatus.FAILED
    run.last_error = "MCP tool 'madad_external_send_whatsapp_template' failed after 3 attempt(s)"
    await runtime.run_store.save(run)

    # User sends the next message (the CR upload they were about to send
    # when the previous call failed). Dispatcher must revive the run, not
    # start a fresh one.
    result = await dispatcher.on_inbound(
        _StubMessage(
            channel=WA,
            identity=IDENTITY,
            text=None,
            attachments=[_StubAttachment("CR.pdf")],
            message_id=None,
        )
    )

    assert result is not None
    # Same run_id — the revive worked, no fresh start.
    assert result.run.run_id == session.active_run_id
    # Run is no longer FAILED in the store.
    revived = await runtime.run_store.get(session.active_run_id)
    assert revived.status != RunStatus.FAILED


async def test_completed_run_is_not_revived(harness) -> None:
    """A successfully-completed run must NOT be silently revived. The
    next inbound starts a fresh run as before — this protects the
    'invoice collection loop' terminal state (db9b4a0) and any other
    intentional completion."""
    runtime = harness.platform.runtime
    dispatcher = OnboardingDispatcher(runtime)

    await runtime.start("onboarding", WA, IDENTITY, input={"trigger": "campaign"})
    await runtime.resume(WA, IDENTITY, message={"text": "YES"})

    session = await runtime.sessions.get(WA, IDENTITY)
    assert session is not None and session.active_run_id
    original_run_id = session.active_run_id
    run = await runtime.run_store.get(original_run_id)
    run.status = RunStatus.COMPLETED
    await runtime.run_store.save(run)

    result = await dispatcher.on_inbound(
        _StubMessage(
            channel=WA, identity=IDENTITY, text="hello again",
            attachments=[], message_id=None,
        )
    )

    assert result is not None
    # New run_id — completed runs trigger a fresh start.
    assert result.run.run_id != original_run_id


def test_revivable_set_is_only_transient_terminal() -> None:
    """Make the policy explicit so a future change can't silently
    expand the revive set to COMPLETED / CANCELLED / DEAD_LETTERED."""
    assert REVIVABLE_TERMINAL_STATUSES == frozenset(
        {RunStatus.FAILED, RunStatus.TIMED_OUT}
    )


# -- tiny duck-types for the dispatcher's on_inbound contract ----------------


class _StubAttachment:
    def __init__(self, filename: str) -> None:
        self.filename = filename
        self.provider_ref = filename


class _StubMessage:
    def __init__(
        self,
        *,
        channel: Channel,
        identity: str,
        text: str | None,
        attachments: list,
        message_id: str | None,
    ) -> None:
        self.channel = channel
        self.identity = identity
        self.text = text
        self.attachments = attachments
        self.message_id = message_id
