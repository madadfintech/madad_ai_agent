"""Enumerations for the Operational Visibility service."""

from __future__ import annotations

from enum import StrEnum


class ActivitySource(StrEnum):
    """Which service an activity originated from."""

    WORKFLOW = "workflow"
    COMMUNICATION = "communication"
    NUDGE = "nudge"
    DOCUMENT = "document"
    CMS = "cms"
    SYSTEM = "system"
