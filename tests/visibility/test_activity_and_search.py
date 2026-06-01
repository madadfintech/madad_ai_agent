"""Activity ingestion, search/filtering, audit visibility, metrics."""

from __future__ import annotations

from app.services.visibility import ActivitySource
from app.services.visibility.persistence import ActivityFilter

from .conftest import activity

WF = ActivitySource.WORKFLOW
COMM = ActivitySource.COMMUNICATION
DOC = ActivitySource.DOCUMENT


async def test_record_and_query_orders_by_time(service):
    await service.record(
        activity(COMM, "communication.message.received", t=2, conversation_id="c1")
    )
    await service.record(activity(WF, "workflow.run.started", t=1, run_id="r1"))

    everything = await service.list_activities()
    assert [a.type for a in everything] == [
        "workflow.run.started",
        "communication.message.received",
    ]


async def test_filter_by_source_and_text(service):
    await service.record(activity(WF, "workflow.run.started", run_id="r1"))
    await service.record(activity(COMM, "communication.message.sent", conversation_id="c1"))

    only_wf = await service.list_activities(ActivityFilter(source=WF))
    assert len(only_wf) == 1 and only_wf[0].source == WF

    matched = await service.list_activities(ActivityFilter(text="sent"))
    assert len(matched) == 1 and matched[0].type.endswith("sent")


async def test_filter_by_refs(service):
    await service.record(
        activity(DOC, "document.received", document_id="d1", application_ref="APP1")
    )
    await service.record(
        activity(DOC, "document.completed", document_id="d2", application_ref="APP2")
    )

    app1 = await service.list_activities(ActivityFilter(application_ref="APP1"))
    assert [a.type for a in app1] == ["document.received"]


async def test_metrics_snapshot(service):
    await service.record(activity(WF, "workflow.run.started"))
    await service.record(activity(WF, "workflow.run.completed"))
    await service.record(activity(COMM, "communication.message.sent"))

    metrics = service.get_metrics()
    assert metrics.total_events == 3
    assert metrics.by_source["workflow"] == 2
    assert metrics.by_source["communication"] == 1
    assert metrics.by_type["workflow.run.started"] == 1
