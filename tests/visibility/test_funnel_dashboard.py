"""Funnel analytics and dashboard aggregation."""

from __future__ import annotations

from app.services.visibility import (
    ActivitySource,
    FunnelConfig,
    FunnelStage,
    build_visibility_service,
)

from .conftest import activity

SYS = ActivitySource.SYSTEM
WF = ActivitySource.WORKFLOW
COMM = ActivitySource.COMMUNICATION
DOC = ActivitySource.DOCUMENT


async def test_funnel_counts_distinct_subjects_per_stage():
    config = FunnelConfig(
        stages=[
            FunnelStage("a", "Stage A", {"x.a"}),
            FunnelStage("b", "Stage B", {"x.b"}),
        ]
    )
    service = build_visibility_service(funnel_config=config)

    await service.record(activity(SYS, "x.a", application_ref="APP1"))
    await service.record(activity(SYS, "x.a", application_ref="APP2"))
    await service.record(activity(SYS, "x.a", application_ref="APP1"))  # duplicate subject
    await service.record(activity(SYS, "x.b", application_ref="APP1"))

    report = service.get_funnel()
    counts = {s.key: s.count for s in report.stages}
    assert counts == {"a": 2, "b": 1}
    assert report.stages[0].conversion == 1.0
    assert report.stages[1].conversion == 0.5


async def test_dashboard_aggregates_across_sources(service):
    await service.record(activity(COMM, "communication.message.received", conversation_id="c1"))
    await service.record(activity(WF, "workflow.run.started", run_id="r1"))
    await service.record(activity(DOC, "document.received", document_id="d1"))

    dashboard = await service.get_dashboard()
    assert dashboard.metrics.total_events == 3
    assert dashboard.conversations == 1
    assert dashboard.workflow_runs == 1
    assert dashboard.documents == 1
