"""Operational Visibility orchestration (read/aggregation + activity ingestion).

Intentionally lightweight for Phase 1: ingest normalized activities, maintain
incremental funnel/metrics projections, and serve read APIs — communication
review log, conversation replay, workflow timelines/summaries, search, and
dashboard aggregation. Conversation replay merges the activity timeline with
message content pulled from Communication via a port.
"""

from __future__ import annotations

from typing import Any

from app.core.logging import get_logger

from .enums import ActivitySource
from .models import (
    ActivityEvent,
    ConversationReplay,
    ConversationSummary,
    DashboardSnapshot,
    FunnelReport,
    MetricsSnapshot,
    ReplayEntry,
    WorkflowSummary,
)
from .persistence import ActivityFilter, ActivityStore
from .projections import FunnelConfig, FunnelProjection, MetricsProjection
from .sources import MessageSource, NullMessageSource

# Workflow event type -> derived run status (for summaries).
_STATUS_BY_EVENT: dict[str, str] = {
    "workflow.run.started": "running",
    "workflow.run.resumed": "running",
    "workflow.run.suspended": "waiting",
    "workflow.run.completed": "completed",
    "workflow.run.failed": "failed",
    "workflow.run.timed_out": "timed_out",
}


class OperationalVisibilityService:
    """Cross-service observability: activity log, projections, and read APIs."""

    def __init__(
        self,
        *,
        store: ActivityStore,
        message_source: MessageSource | None = None,
        funnel_config: FunnelConfig | None = None,
        logger: Any | None = None,
    ) -> None:
        self._store = store
        self._messages = message_source or NullMessageSource()
        self._metrics = MetricsProjection()
        self._funnel = FunnelProjection(funnel_config)
        self._log = logger or get_logger("visibility.service")

    # -- ingestion ------------------------------------------------------------

    async def record(self, activity: ActivityEvent) -> ActivityEvent:
        """Ingest one normalized activity and update projections."""

        await self._store.append(activity)
        self._metrics.update(activity)
        self._funnel.update(activity)
        return activity

    # -- search / audit visibility -------------------------------------------

    async def list_activities(
        self, filt: ActivityFilter | None = None, *, limit: int = 100, offset: int = 0
    ) -> list[ActivityEvent]:
        return await self._store.query(filt or ActivityFilter(), limit=limit, offset=offset)

    # -- communication review log + replay -----------------------------------

    async def list_conversations(self, *, limit: int = 100) -> list[ConversationSummary]:
        activities = await self._store.query(
            ActivityFilter(source=ActivitySource.COMMUNICATION), limit=10_000
        )
        summaries: dict[str, ConversationSummary] = {}
        for activity in activities:
            cid = activity.conversation_id
            if cid is None:
                continue
            summary = summaries.get(cid)
            if summary is None:
                summary = ConversationSummary(
                    conversation_id=cid, identity=activity.identity, channel=activity.channel
                )
                summaries[cid] = summary
            summary.activity_count += 1
            summary.last_activity_at = activity.occurred_at
        ordered = sorted(
            summaries.values(),
            key=lambda s: s.last_activity_at or s.conversation_id,
            reverse=True,
        )
        return ordered[:limit]

    async def get_conversation_log(self, conversation_id: str) -> list[ActivityEvent]:
        return await self._store.query(
            ActivityFilter(conversation_id=conversation_id), limit=10_000
        )

    async def replay_conversation(self, conversation_id: str) -> ConversationReplay:
        """Merge message content (from Communication) with the activity timeline."""

        messages = await self._messages.get_conversation_messages(conversation_id)
        activities = await self.get_conversation_log(conversation_id)

        entries: list[ReplayEntry] = []
        for message in messages:
            entries.append(
                ReplayEntry(
                    kind="message",
                    occurred_at=message.occurred_at,
                    source="communication",
                    summary=message.text or f"[{message.type}]",
                    detail={
                        "direction": message.direction,
                        "status": message.status,
                        "channel": message.channel,
                    },
                )
            )
        for activity in activities:
            # Skip per-message comm events when we already have the message itself.
            if messages and activity.source == ActivitySource.COMMUNICATION:
                continue
            entries.append(
                ReplayEntry(
                    kind="event",
                    occurred_at=activity.occurred_at,
                    source=str(activity.source),
                    summary=activity.summary or activity.type,
                    detail={"type": activity.type},
                )
            )
        entries.sort(key=lambda e: e.occurred_at)
        return ConversationReplay(
            conversation_id=conversation_id,
            entries=entries,
            message_count=len(messages),
            event_count=len(activities),
        )

    # -- workflow visibility / history / summaries ---------------------------

    async def get_workflow_timeline(self, run_id: str) -> list[ActivityEvent]:
        return await self._store.query(ActivityFilter(run_id=run_id), limit=10_000)

    async def list_workflow_runs(self, *, limit: int = 100) -> list[WorkflowSummary]:
        activities = await self._store.query(
            ActivityFilter(source=ActivitySource.WORKFLOW), limit=10_000
        )
        runs: dict[str, list[ActivityEvent]] = {}
        for activity in activities:
            if activity.run_id is None:
                continue
            runs.setdefault(activity.run_id, []).append(activity)
        summaries = [self._summarize_run(run_id, evts) for run_id, evts in runs.items()]
        summaries.sort(key=lambda s: s.last_event_at or s.run_id, reverse=True)
        return summaries[:limit]

    async def get_workflow_summary(self, run_id: str) -> WorkflowSummary:
        activities = await self.get_workflow_timeline(run_id)
        return self._summarize_run(run_id, activities)

    @staticmethod
    def _summarize_run(run_id: str, activities: list[ActivityEvent]) -> WorkflowSummary:
        ordered = sorted(activities, key=lambda a: a.occurred_at)
        status = "running"
        for activity in ordered:
            status = _STATUS_BY_EVENT.get(activity.type, status)
        first = ordered[0] if ordered else None
        last = ordered[-1] if ordered else None
        return WorkflowSummary(
            run_id=run_id,
            workflow=first.workflow if first else None,
            session_id=first.session_id if first else None,
            status=status,
            event_count=len(ordered),
            started_at=first.occurred_at if first else None,
            last_event_at=last.occurred_at if last else None,
            last_event=last.type if last else None,
        )

    # -- metrics / funnel / dashboard ----------------------------------------

    def get_metrics(self) -> MetricsSnapshot:
        return self._metrics.snapshot()

    def get_funnel(self) -> FunnelReport:
        return self._funnel.report()

    async def get_dashboard(self) -> DashboardSnapshot:
        metrics = self._metrics.snapshot()
        conversations = len(await self.list_conversations(limit=10_000))
        runs = len(await self.list_workflow_runs(limit=10_000))
        documents = metrics.by_source.get(str(ActivitySource.DOCUMENT), 0)
        return DashboardSnapshot(
            metrics=metrics,
            funnel=self._funnel.report(),
            conversations=conversations,
            workflow_runs=runs,
            documents=documents,
        )
