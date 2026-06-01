"""Operational Visibility Service FastAPI app (Application Server container, port 8006).

Backend read APIs only (no UI): communication review log + conversation replay,
workflow visibility/history/summaries, search & audit visibility, and dashboard
aggregation (metrics + funnel). ``POST /visibility/activities`` is the ingestion
endpoint used by event consumers in production.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import Depends

from app.core.app import create_service_app

from .deps import get_visibility_service
from .enums import ActivitySource
from .models import (
    ActivityEvent,
    ConversationReplay,
    ConversationSummary,
    DashboardSnapshot,
    FunnelReport,
    MetricsSnapshot,
    WorkflowSummary,
)
from .persistence import ActivityFilter
from .service import OperationalVisibilityService

# Admin/ops service (ops.ai.madadfintech.com): gated by the admin bearer token.
# Visibility is the event CONSUMER (see consumer.py for the cross-process
# drain), so it forwards nothing — no producer lifespan here.
app = create_service_app(
    title="MADAD Operational Visibility Service", service="visibility", admin=True
)

Service = Annotated[OperationalVisibilityService, Depends(get_visibility_service)]


@app.post("/visibility/activities", response_model=ActivityEvent)
async def ingest_activity(activity: ActivityEvent, service: Service) -> ActivityEvent:
    return await service.record(activity)


@app.get("/visibility/activities", response_model=list[ActivityEvent])
async def search_activities(
    service: Service,
    source: ActivitySource | None = None,
    type: str | None = None,
    conversation_id: str | None = None,
    run_id: str | None = None,
    application_ref: str | None = None,
    identity: str | None = None,
    text: str | None = None,
    limit: int = 100,
    offset: int = 0,
) -> list[ActivityEvent]:
    filt = ActivityFilter(
        source=source,
        type=type,
        conversation_id=conversation_id,
        run_id=run_id,
        application_ref=application_ref,
        identity=identity,
        text=text,
    )
    return await service.list_activities(filt, limit=limit, offset=offset)


@app.get("/visibility/conversations", response_model=list[ConversationSummary])
async def list_conversations(service: Service) -> list[ConversationSummary]:
    return await service.list_conversations()


@app.get(
    "/visibility/conversations/{conversation_id}/log",
    response_model=list[ActivityEvent],
)
async def conversation_log(conversation_id: str, service: Service) -> list[ActivityEvent]:
    return await service.get_conversation_log(conversation_id)


@app.get(
    "/visibility/conversations/{conversation_id}/replay",
    response_model=ConversationReplay,
)
async def conversation_replay(conversation_id: str, service: Service) -> ConversationReplay:
    return await service.replay_conversation(conversation_id)


@app.get("/visibility/workflows", response_model=list[WorkflowSummary])
async def list_workflow_runs(service: Service) -> list[WorkflowSummary]:
    return await service.list_workflow_runs()


@app.get("/visibility/workflows/{run_id}/timeline", response_model=list[ActivityEvent])
async def workflow_timeline(run_id: str, service: Service) -> list[ActivityEvent]:
    return await service.get_workflow_timeline(run_id)


@app.get("/visibility/workflows/{run_id}/summary", response_model=WorkflowSummary)
async def workflow_summary(run_id: str, service: Service) -> WorkflowSummary:
    return await service.get_workflow_summary(run_id)


@app.get("/visibility/metrics", response_model=MetricsSnapshot)
async def metrics(service: Service) -> MetricsSnapshot:
    return service.get_metrics()


@app.get("/visibility/funnel", response_model=FunnelReport)
async def funnel(service: Service) -> FunnelReport:
    return service.get_funnel()


@app.get("/visibility/dashboard", response_model=DashboardSnapshot)
async def dashboard(service: Service) -> DashboardSnapshot:
    return await service.get_dashboard()
