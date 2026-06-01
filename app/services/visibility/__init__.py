"""MADAD Operational Visibility Service.

Intentionally lightweight for Phase 1: a cross-service observability layer that
ingests normalized activities from every service, maintains incremental funnel
and metrics projections, and serves read APIs — communication review logs,
conversation replay, workflow visibility/history/summaries, search & audit
visibility, and dashboard aggregation. Backend APIs only.
"""

from __future__ import annotations

from .bridges import (
    cms_to_activity,
    communication_to_activity,
    document_to_activity,
    nudge_to_activity,
    subscribe_cms,
    subscribe_communication,
    subscribe_document,
    subscribe_nudge,
    subscribe_workflow,
    workflow_to_activity,
)
from .deps import build_visibility_service, get_visibility_service
from .enums import ActivitySource
from .models import (
    ActivityEvent,
    ConversationReplay,
    ConversationSummary,
    DashboardSnapshot,
    FunnelReport,
    FunnelStageReport,
    MetricsSnapshot,
    ReplayEntry,
    ReplayMessage,
    WorkflowSummary,
)
from .persistence import ActivityFilter, ActivityStore, InMemoryActivityStore
from .projections import (
    DEFAULT_ONBOARDING_FUNNEL,
    FunnelConfig,
    FunnelProjection,
    FunnelStage,
    MetricsProjection,
)
from .service import OperationalVisibilityService
from .sources import (
    CommunicationMessageSource,
    InMemoryMessageSource,
    MessageSource,
    NullMessageSource,
)

__all__ = [
    # service
    "OperationalVisibilityService",
    "build_visibility_service",
    "get_visibility_service",
    # enums
    "ActivitySource",
    # models
    "ActivityEvent",
    "ConversationReplay",
    "ConversationSummary",
    "ReplayEntry",
    "ReplayMessage",
    "WorkflowSummary",
    "FunnelReport",
    "FunnelStageReport",
    "MetricsSnapshot",
    "DashboardSnapshot",
    # persistence
    "ActivityStore",
    "InMemoryActivityStore",
    "ActivityFilter",
    # projections
    "MetricsProjection",
    "FunnelProjection",
    "FunnelConfig",
    "FunnelStage",
    "DEFAULT_ONBOARDING_FUNNEL",
    # sources
    "MessageSource",
    "InMemoryMessageSource",
    "CommunicationMessageSource",
    "NullMessageSource",
    # bridges
    "subscribe_workflow",
    "subscribe_communication",
    "subscribe_nudge",
    "subscribe_document",
    "subscribe_cms",
    "workflow_to_activity",
    "communication_to_activity",
    "nudge_to_activity",
    "document_to_activity",
    "cms_to_activity",
]
