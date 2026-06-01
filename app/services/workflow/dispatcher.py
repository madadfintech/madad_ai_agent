"""Dispatcher — bridges inbound channel messages and webhooks to the runtime.

* :meth:`on_inbound` implements the Communication service's ``ConversationDispatcher``
  seam: the first message starts the onboarding workflow (campaign entry / organic
  contact); later messages resume the waiting run (reconnect recovery is free).
* :meth:`resume_external` resumes a run waiting on an external decision
  (pre-qualification, score, payment, offers, selection) from a webhook payload.
"""

from __future__ import annotations

from typing import Any

from app.shared.workflow import Channel, ExecutionResult, WorkflowRuntime
from app.shared.workflow.enums import TERMINAL_STATUSES

DEFAULT_WORKFLOW = "onboarding"


class OnboardingDispatcher:
    """Routes inbound messages and webhooks into the onboarding workflow."""

    def __init__(self, runtime: WorkflowRuntime, *, workflow: str = DEFAULT_WORKFLOW) -> None:
        self._runtime = runtime
        self._workflow = workflow

    async def on_inbound(self, message: Any) -> ExecutionResult:
        """Start or resume the workflow for an inbound channel message.

        ``message`` is a communication ``Message`` (duck-typed: channel, identity,
        text, attachments).
        """

        channel = message.channel
        identity = message.identity
        payload = {
            "text": message.text,
            "attachments": [
                {"filename": a.filename, "provider_ref": a.provider_ref}
                for a in getattr(message, "attachments", [])
            ],
            "message_id": getattr(message, "message_id", None),
        }
        return await self._dispatch(channel, identity, payload)

    async def inbound(
        self,
        channel: Channel,
        identity: str,
        *,
        text: str | None = None,
        attachments: list[dict[str, Any]] | None = None,
    ) -> ExecutionResult:
        """Start/resume from a normalized inbound message (API/testing entry)."""

        return await self._dispatch(
            channel, identity, {"text": text, "attachments": attachments or []}
        )

    async def resume_external(
        self, channel: Channel, identity: str, payload: dict[str, Any]
    ) -> ExecutionResult:
        """Resume a run waiting on an external (webhook) decision."""

        return await self._runtime.resume(channel, identity, message=payload)

    async def _dispatch(
        self, channel: Channel, identity: str, payload: dict[str, Any]
    ) -> ExecutionResult:
        session = await self._runtime.sessions.get(channel, identity)
        if session is not None and session.active_run_id:
            run = await self._runtime.run_store.get_or_none(session.active_run_id)
            if run is not None and run.status not in TERMINAL_STATUSES:
                return await self._runtime.resume(channel, identity, message=payload)
        return await self._runtime.start(self._workflow, channel, identity, input=payload)
