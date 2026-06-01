"""MADAD Nudge & Notification Service.

Lifecycle reminders and operational notifications: inactivity reminders, due
alerts, repayment reminders, workflow-recovery nudges, and escalations. This
service is pure orchestration — it decides *when* and *what* to remind. Delivery
goes through the Communication service (which renders CMS templates and dispatches
via the MCP cluster); timings/channels/content are sourced from the CMS, so
reminders are runtime-configurable with no deploy.
"""

from __future__ import annotations

from .audit import NudgeAuditEntry, NudgeAuditLogger
from .config_provider import (
    CmsNudgeConfigProvider,
    InMemoryNudgeConfigProvider,
    NudgeConfigProvider,
)
from .deps import build_nudge_service, get_nudge_service
from .dispatcher import (
    CommunicationNotificationDispatcher,
    InMemoryNotificationDispatcher,
    NotificationDispatcher,
)
from .enums import ReminderStatus, SequenceStatus
from .errors import (
    NotificationDispatchError,
    NudgeError,
    ScheduleNotFoundError,
    SequenceNotFoundError,
)
from .events import (
    InMemoryNudgeEventBus,
    NudgeEvent,
    NudgeEventBus,
    NudgeEventType,
)
from .models import NudgeScheduleConfig, NudgeSequence, NudgeStep, Reminder
from .persistence import InMemoryNudgeStore, NudgeStore
from .service import NudgeConfig, NudgeService

__all__ = [
    # service
    "NudgeService",
    "NudgeConfig",
    "build_nudge_service",
    "get_nudge_service",
    # enums
    "SequenceStatus",
    "ReminderStatus",
    # models
    "NudgeStep",
    "NudgeScheduleConfig",
    "NudgeSequence",
    "Reminder",
    # config provider
    "NudgeConfigProvider",
    "InMemoryNudgeConfigProvider",
    "CmsNudgeConfigProvider",
    # dispatcher
    "NotificationDispatcher",
    "InMemoryNotificationDispatcher",
    "CommunicationNotificationDispatcher",
    # persistence
    "NudgeStore",
    "InMemoryNudgeStore",
    # events
    "NudgeEvent",
    "NudgeEventType",
    "NudgeEventBus",
    "InMemoryNudgeEventBus",
    # audit
    "NudgeAuditLogger",
    "NudgeAuditEntry",
    # errors
    "NudgeError",
    "ScheduleNotFoundError",
    "SequenceNotFoundError",
    "NotificationDispatchError",
]
