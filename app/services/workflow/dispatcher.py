"""Dispatcher — bridges inbound channel messages and webhooks to the runtime.

* :meth:`on_inbound` implements the Communication service's ``ConversationDispatcher``
  seam: the first message starts the onboarding workflow (campaign entry /
  organic contact); later messages resume the waiting run (reconnect recovery
  is free).
* :meth:`resume_external` resumes a run waiting on an external decision from
  a raw payload (test / internal-API entry).
* :meth:`on_backend_event` is the single chokepoint for backend webhook
  events: validates the event type, dedupes on ``event_id``, translates the
  payload into the workflow's resume shape, and resumes. This is the only
  entry route that production webhook receivers should call.
"""

from __future__ import annotations

from typing import Any

from app.shared.workflow import Channel, ExecutionResult, WorkflowRuntime
from app.shared.workflow.enums import TERMINAL_STATUSES, RunStatus
from app.shared.workflow.errors import WorkflowExecutionError

from .webhook_dedupe import InMemoryWebhookDedupe, WebhookDedupe

DEFAULT_WORKFLOW = "onboarding"

# QA #2 (2026-06-09): runs that died from a transient failure (network
# blip, backend 403, MCP timeout) should be revived on the next user
# message instead of starting from scratch. The LangGraph checkpoint
# still holds the last parked-step state — we just need to re-transition
# the run to RUNNING and let the executor replay the new inbound at the
# pending interrupt. COMPLETED / CANCELLED / DEAD_LETTERED are intentionally
# NOT in this set (the first is success, the other two are operator-set
# kills that should not silently un-do themselves).
REVIVABLE_TERMINAL_STATUSES: frozenset[RunStatus] = frozenset(
    {RunStatus.FAILED, RunStatus.TIMED_OUT}
)


# -- Canonical backend event types -------------------------------------------
#
# The Phase 4 webhook receivers accept any event type in this combined set.
# Phase 1.a events drive the active onboarding flow; Phase 1.b events are
# registered NOW (so the receiver path can already accept them) but their
# workflow handlers don't land until Phase 6 (invoice financing). Per
# [[project_mcp_catalog]] § "WEBHOOK EVENTS".

PHASE1A_BACKEND_EVENTS: frozenset[str] = frozenset(
    {
        "eligibility.updated",
        "documents.completed",
        "prequalification.completed",
        "madad_score.ready",
        "payment.completed",
        "offers.available",
        "offer.accepted",
        "credit_line.activated",
    }
)

PHASE1B_BACKEND_EVENTS: frozenset[str] = frozenset(
    {
        "transaction.disbursed",
        "repayment.received",
        "repayment.partially_paid",
        "repayment.closed",
        "repayment.due_soon",
        "repayment.overdue",
    }
)

ALL_BACKEND_EVENTS: frozenset[str] = PHASE1A_BACKEND_EVENTS | PHASE1B_BACKEND_EVENTS


# Map well-known event types to the journey_status they imply. The workflow
# uses this hint to advance immediately without an extra auth_me round-trip
# — useful in staging where the operator-issued event is the truth, and
# essential for demos where the test account's backend state doesn't change
# between webhook posts.
EVENT_TO_JOURNEY_STATUS: dict[str, str] = {
    "eligibility.updated": "PRE_QUALIFIED",
    "documents.completed": "PRE_QUALIFIED",
    "prequalification.completed": "PRE_QUALIFIED",
    "madad_score.ready": "QUALIFIED",
    "offers.available": "ACCEPTED",
    "offer.accepted": "OFFER_ACCEPTED",
    "credit_line.activated": "ACTIVATED",
}


def translate_backend_event(
    event_type: str, payload: dict[str, Any]
) -> dict[str, Any]:
    """Translate a backend event into the workflow's resume payload shape.

    ``payment.completed`` resumes ``payment_await`` with ``paid=True`` so
    ``_route_payment`` advances to ``lender_status_poll``. Status-update
    events resume ``journey_wait_await`` / ``lender_wait_await``;
    well-known events also carry the implied ``journey_status`` so the
    workflow can advance without a separate auth_me round-trip.
    ``last_status_source="webhook"`` rides along so the polling worker
    can suppress its next cycle for this run.
    """

    base: dict[str, Any] = {"last_status_source": "webhook"}
    if event_type == "payment.completed":
        return {"type": "payment", "paid": True, **base, **payload}
    if event_type in EVENT_TO_JOURNEY_STATUS:
        base["journey_status"] = EVENT_TO_JOURNEY_STATUS[event_type]
    return {"type": "status_update", "event": event_type, **base, **payload}


class OnboardingDispatcher:
    """Routes inbound messages and webhooks into the onboarding workflow."""

    def __init__(
        self,
        runtime: WorkflowRuntime,
        *,
        workflow: str = DEFAULT_WORKFLOW,
        dedupe: WebhookDedupe | None = None,
        allowed_event_types: frozenset[str] | set[str] | None = None,
    ) -> None:
        self._runtime = runtime
        self._workflow = workflow
        self._dedupe = dedupe or InMemoryWebhookDedupe()
        self._allowed = frozenset(
            allowed_event_types if allowed_event_types is not None else ALL_BACKEND_EVENTS
        )

    @property
    def allowed_event_types(self) -> frozenset[str]:
        return self._allowed

    async def on_inbound(self, message: Any) -> ExecutionResult | None:
        """Start or resume the workflow for an inbound channel message.

        ``message`` is a communication ``Message`` (duck-typed: channel,
        identity, text, attachments). Returns None when a non-None
        ``message_id`` is rejected by the dedupe layer (caller responds 200
        so the source bridge does not retry).
        """

        channel = message.channel
        identity = message.identity
        message_id = getattr(message, "message_id", None)
        payload = {
            "text": message.text,
            "attachments": [
                {"filename": a.filename, "provider_ref": a.provider_ref}
                for a in getattr(message, "attachments", [])
            ],
            "message_id": message_id,
        }
        return await self._dispatch(channel, identity, payload, message_id=message_id)

    async def inbound(
        self,
        channel: Channel,
        identity: str,
        *,
        text: str | None = None,
        attachments: list[dict[str, Any]] | None = None,
        data: dict[str, Any] | None = None,
        message_id: str | None = None,
    ) -> ExecutionResult | None:
        """Start/resume from a normalized inbound message (API/testing entry).

        ``data`` carries an arbitrary structured payload (eligibility form,
        buyer / shareholder capture); its keys are merged into the resume
        payload at the top level so the workflow's form-await nodes see
        them directly.

        ``message_id`` is the source bridge's unique id for the inbound
        message (Meta's ``wamid``, SendGrid's ``Message-ID``, etc.). When
        supplied, the call is deduplicated through the shared dedupe layer
        with the prefix ``inbound:`` so the same Meta payload re-delivered
        on a network retry is not re-played through the workflow. Returns
        None on duplicate.
        """

        payload: dict[str, Any] = {"text": text, "attachments": attachments or []}
        if data:
            payload.update(data)
        return await self._dispatch(channel, identity, payload, message_id=message_id)

    async def resume_external(
        self, channel: Channel, identity: str, payload: dict[str, Any]
    ) -> ExecutionResult:
        """Resume a run waiting on an external (webhook) decision."""

        return await self._runtime.resume(channel, identity, message=payload)

    async def on_backend_event(
        self,
        *,
        event_type: str,
        event_id: str | None,
        channel: Channel,
        identity: str,
        payload: dict[str, Any],
    ) -> ExecutionResult | None:
        """Single chokepoint for backend webhook events.

        Returns ``None`` if the event was a duplicate (the dedupe rejected the
        id) — production receivers should respond 200 either way so the
        backend doesn't retry. ``UnknownEventTypeError`` is raised for an
        unknown event type so the receiver can respond with 400.
        """

        if event_type not in self._allowed:
            raise UnknownEventTypeError(event_type)
        if event_id is not None and not await self._dedupe.claim(event_id):
            return None
        resume_payload = translate_backend_event(event_type, payload)
        return await self.resume_external(channel, identity, resume_payload)

    async def _dispatch(
        self,
        channel: Channel,
        identity: str,
        payload: dict[str, Any],
        *,
        message_id: str | None = None,
    ) -> ExecutionResult | None:
        if message_id is not None and not await self._dedupe.claim(f"inbound:{message_id}"):
            return None
        session = await self._runtime.sessions.get(channel, identity)
        if session is not None and session.active_run_id:
            run = await self._runtime.run_store.get_or_none(session.active_run_id)
            if run is not None:
                if run.status not in TERMINAL_STATUSES:
                    return await self._runtime.resume(channel, identity, message=payload)
                if run.status in REVIVABLE_TERMINAL_STATUSES:
                    # QA #2: a transient failure left this run terminally
                    # dead in the store, but the LangGraph checkpoint at
                    # the last successful interrupt is still intact. Flip
                    # the run record back to RUNNING and replay the new
                    # inbound — if the resume itself errors (genuinely
                    # broken state), fall through to a fresh start so the
                    # SME is never stuck.
                    try:
                        await self._runtime.revive_failed_run(run)
                        return await self._runtime.resume(
                            channel, identity, message=payload
                        )
                    except (WorkflowExecutionError, Exception):  # noqa: BLE001
                        pass
        return await self._runtime.start(self._workflow, channel, identity, input=payload)


class UnknownEventTypeError(Exception):
    """Raised by :meth:`OnboardingDispatcher.on_backend_event` when the
    backend posts an event type the dispatcher's allowed set doesn't
    recognise."""

    def __init__(self, event_type: str) -> None:
        super().__init__(f"unknown backend event type: {event_type!r}")
        self.event_type = event_type
