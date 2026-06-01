"""Operational Visibility models.

``ActivityEvent`` is the normalized, cross-service record the whole service is
built on — every event from every service maps to one. The rest are read models
returned by the APIs (replay, summaries, funnel, metrics, dashboard).
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field

from app.shared.workflow.utils import new_id, utcnow

from .enums import ActivitySource


class ActivityEvent(BaseModel):
    """A normalized observability record from any service."""

    activity_id: str = Field(default_factory=lambda: new_id("act"))
    source: ActivitySource
    type: str
    occurred_at: datetime = Field(default_factory=utcnow)

    # Optional correlation refs (used for filtering, timelines, funnel subjects).
    session_id: str | None = None
    conversation_id: str | None = None
    run_id: str | None = None
    document_id: str | None = None
    batch_id: str | None = None
    application_ref: str | None = None
    identity: str | None = None
    channel: str | None = None
    workflow: str | None = None

    summary: str | None = None
    payload: dict[str, Any] = Field(default_factory=dict)

    def subject(self) -> str | None:
        """The funnel subject key — the most stable identity available."""

        return self.application_ref or self.session_id or self.identity or self.conversation_id


# -- replay ------------------------------------------------------------------


class ReplayMessage(BaseModel):
    """A message snapshot for conversation replay (content pulled from Comms)."""

    message_id: str
    direction: str
    type: str
    status: str
    text: str | None = None
    channel: str | None = None
    occurred_at: datetime


class ReplayEntry(BaseModel):
    """One chronological entry in a conversation replay (message or event)."""

    kind: str  # "message" | "event"
    occurred_at: datetime
    source: str
    summary: str | None = None
    detail: dict[str, Any] = Field(default_factory=dict)


class ConversationReplay(BaseModel):
    conversation_id: str
    entries: list[ReplayEntry] = Field(default_factory=list)
    message_count: int = 0
    event_count: int = 0


class ConversationSummary(BaseModel):
    conversation_id: str
    identity: str | None = None
    channel: str | None = None
    activity_count: int = 0
    last_activity_at: datetime | None = None


# -- workflow visibility -----------------------------------------------------


class WorkflowSummary(BaseModel):
    run_id: str
    workflow: str | None = None
    session_id: str | None = None
    status: str = "running"
    event_count: int = 0
    started_at: datetime | None = None
    last_event_at: datetime | None = None
    last_event: str | None = None


# -- funnel / metrics / dashboard --------------------------------------------


class FunnelStageReport(BaseModel):
    key: str
    label: str
    count: int
    conversion: float | None = None  # vs the first stage


class FunnelReport(BaseModel):
    stages: list[FunnelStageReport] = Field(default_factory=list)


class MetricsSnapshot(BaseModel):
    total_events: int = 0
    by_source: dict[str, int] = Field(default_factory=dict)
    by_type: dict[str, int] = Field(default_factory=dict)


class DashboardSnapshot(BaseModel):
    metrics: MetricsSnapshot
    funnel: FunnelReport
    conversations: int = 0
    workflow_runs: int = 0
    documents: int = 0
