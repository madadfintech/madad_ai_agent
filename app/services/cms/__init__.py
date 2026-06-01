"""MADAD CMS & Dynamic Configuration Service.

Lets MADAD operators update templates, workflow configs, document checklists,
nudge timings, and multilingual content at runtime — no engineering, no deploy.
Changes propagate to live conversations within the cache TTL / on invalidation
(the <5-minute Milestone 1 requirement). Versioned with rollback, validated per
kind, audited, and event-emitting. Backend APIs only.
"""

from __future__ import annotations

from .audit import CmsAuditEntry, CmsAuditLogger
from .cache import DEFAULT_TTL_SECONDS, ConfigCache, InMemoryConfigCache
from .deps import build_cms_service, get_cms_service
from .enums import ConfigKind
from .errors import (
    CmsError,
    ConfigNotFoundError,
    ConfigValidationError,
    ConfigVersionNotFoundError,
)
from .events import CmsEvent, CmsEventBus, CmsEventType, InMemoryCmsEventBus
from .models import (
    ChecklistItem,
    ConfigKey,
    ConfigRecord,
    ConfigVersion,
    checklist_value,
    parse_checklist,
)
from .persistence import ConfigStore, InMemoryConfigStore
from .service import GLOBAL_VARIABLES, CmsService
from .templating_bridge import CmsTemplateProvider
from .validation import register_validator, validate

__all__ = [
    # service
    "CmsService",
    "build_cms_service",
    "get_cms_service",
    "GLOBAL_VARIABLES",
    # enums
    "ConfigKind",
    # models
    "ConfigKey",
    "ConfigRecord",
    "ConfigVersion",
    "ChecklistItem",
    "checklist_value",
    "parse_checklist",
    # persistence
    "ConfigStore",
    "InMemoryConfigStore",
    # cache
    "ConfigCache",
    "InMemoryConfigCache",
    "DEFAULT_TTL_SECONDS",
    # events
    "CmsEvent",
    "CmsEventType",
    "CmsEventBus",
    "InMemoryCmsEventBus",
    # audit
    "CmsAuditLogger",
    "CmsAuditEntry",
    # validation
    "validate",
    "register_validator",
    # bridge
    "CmsTemplateProvider",
    # errors
    "CmsError",
    "ConfigNotFoundError",
    "ConfigVersionNotFoundError",
    "ConfigValidationError",
]
