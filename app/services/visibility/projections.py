"""Incremental projections — metrics counters and onboarding funnel.

Maintained on each activity so dashboard/funnel reads are O(1) rather than
scanning the log. The funnel is fully configurable (stage -> matching activity
types); a sensible onboarding default is provided.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field

from .models import ActivityEvent, FunnelReport, FunnelStageReport, MetricsSnapshot


class MetricsProjection:
    """Running counts of activities by source and type."""

    def __init__(self) -> None:
        self._total = 0
        self._by_source: dict[str, int] = defaultdict(int)
        self._by_type: dict[str, int] = defaultdict(int)

    def update(self, activity: ActivityEvent) -> None:
        self._total += 1
        self._by_source[str(activity.source)] += 1
        self._by_type[activity.type] += 1

    def snapshot(self) -> MetricsSnapshot:
        return MetricsSnapshot(
            total_events=self._total,
            by_source=dict(self._by_source),
            by_type=dict(self._by_type),
        )


@dataclass
class FunnelStage:
    key: str
    label: str
    types: set[str]  # activity types that mark this stage as reached


@dataclass
class FunnelConfig:
    stages: list[FunnelStage] = field(default_factory=list)


# Default onboarding funnel, keyed to events the platform emits today. Replace via
# CMS/config when the Steps 0–12 business workflows land.
DEFAULT_ONBOARDING_FUNNEL = FunnelConfig(
    stages=[
        FunnelStage("interest", "Interest", {"communication.message.received"}),
        FunnelStage("started", "Onboarding started", {"workflow.run.started"}),
        FunnelStage("docs_received", "Documents received", {"document.received"}),
        FunnelStage("docs_validated", "Documents validated", {"document.completed"}),
        FunnelStage("completed", "Onboarding completed", {"workflow.run.completed"}),
    ]
)


class FunnelProjection:
    """Tracks the set of distinct subjects that reached each funnel stage."""

    def __init__(self, config: FunnelConfig | None = None) -> None:
        self._config = config or DEFAULT_ONBOARDING_FUNNEL
        self._reached: dict[str, set[str]] = {s.key: set() for s in self._config.stages}
        self._type_index: dict[str, list[str]] = defaultdict(list)
        for stage in self._config.stages:
            for activity_type in stage.types:
                self._type_index[activity_type].append(stage.key)

    def update(self, activity: ActivityEvent) -> None:
        stage_keys = self._type_index.get(activity.type)
        if not stage_keys:
            return
        subject = activity.subject()
        if subject is None:
            return
        for key in stage_keys:
            self._reached[key].add(subject)

    def report(self) -> FunnelReport:
        stages = self._config.stages
        first_count = len(self._reached[stages[0].key]) if stages else 0
        reports = []
        for stage in stages:
            count = len(self._reached[stage.key])
            conversion = (count / first_count) if first_count else None
            reports.append(
                FunnelStageReport(
                    key=stage.key, label=stage.label, count=count, conversion=conversion
                )
            )
        return FunnelReport(stages=reports)
