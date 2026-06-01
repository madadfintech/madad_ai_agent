"""Enumerations for the CMS & dynamic configuration service."""

from __future__ import annotations

from enum import StrEnum


class ConfigKind(StrEnum):
    """The kind of managed configuration.

    A single versioned store backs all kinds; the kind selects the validator and
    the typed accessors (templates, checklists, ...).
    """

    TEMPLATE = "template"  # localized, channel-aware conversational content
    WORKFLOW = "workflow"  # workflow parameters / feature flags
    CHECKLIST = "checklist"  # dynamic document requirements
    NUDGE = "nudge"  # reminder timing schedules
    SETTING = "setting"  # generic key/value (e.g. global runtime variables)
