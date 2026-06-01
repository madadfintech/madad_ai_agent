"""Conversation dispatcher seam.

The communication service is responsible for *transport*; deciding what the
conversation should do next (start/resume a workflow) is the Workflow service's
job. ``ConversationDispatcher`` is the seam between them: on an inbound message
the communication service hands off through this port.

Default is a no-op — wiring a real dispatcher (e.g. one that resumes the workflow
runtime) happens at the service-composition layer, keeping this service decoupled
from conversation logic.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from .models import Message


@runtime_checkable
class ConversationDispatcher(Protocol):
    """Handles an inbound message after it has been persisted."""

    async def on_inbound(self, message: Message) -> None: ...


class NoopConversationDispatcher:
    """Default dispatcher: does nothing (consumers react to events instead)."""

    async def on_inbound(self, message: Message) -> None:
        return None
