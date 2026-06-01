"""Unified cross-process event envelope.

Every service has its own typed in-process event (``WorkflowEvent``,
``NudgeEvent``, ...). :class:`Event` is the single normalized envelope that
crosses process boundaries on the unified bus, so a cross-process consumer
(Operational Visibility) reads one shape regardless of origin.

It is intentionally a superset of every service's correlation refs; any subset
may be present. Source-specific fields the envelope doesn't model are carried in
``payload`` by the forwarder.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

from app.shared.workflow.utils import new_id, utcnow


class Event(BaseModel):
    """An immutable, normalized record of something that happened anywhere."""

    event_id: str = Field(default_factory=lambda: new_id("uevt"))
    type: str
    source: str  # ActivitySource value: workflow | communication | nudge | document | cms
    occurred_at: str = Field(default_factory=lambda: utcnow().isoformat())

    # Correlation refs — any subset present, used for filtering/timelines/funnel.
    session_id: str | None = None
    conversation_id: str | None = None
    run_id: str | None = None
    document_id: str | None = None
    batch_id: str | None = None
    application_ref: str | None = None
    identity: str | None = None
    channel: str | None = None
    workflow: str | None = None
    correlation_id: str | None = None

    summary: str | None = None
    payload: dict[str, Any] = Field(default_factory=dict)
