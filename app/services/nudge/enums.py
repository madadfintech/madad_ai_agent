"""Enumerations for the nudge & notification service."""

from __future__ import annotations

from enum import StrEnum


class SequenceStatus(StrEnum):
    """Lifecycle of a nudge sequence (one reminder campaign for a target+reason)."""

    ACTIVE = "active"
    SUPPRESSED = "suppressed"  # awaited action happened — stop reminding
    COMPLETED = "completed"  # all steps delivered
    ESCALATED = "escalated"  # raised to ops (terminal)
    CANCELLED = "cancelled"


SEQUENCE_TERMINAL: frozenset[SequenceStatus] = frozenset(
    {
        SequenceStatus.SUPPRESSED,
        SequenceStatus.COMPLETED,
        SequenceStatus.ESCALATED,
        SequenceStatus.CANCELLED,
    }
)


class ReminderStatus(StrEnum):
    """Lifecycle of an individual scheduled reminder (one step)."""

    SCHEDULED = "scheduled"
    RETRYING = "retrying"
    SENT = "sent"
    FAILED = "failed"  # delivery exhausted retries
    SUPPRESSED = "suppressed"
    CANCELLED = "cancelled"


# Reminders the worker tick should pick up.
REMINDER_DUE_STATUSES: frozenset[ReminderStatus] = frozenset(
    {ReminderStatus.SCHEDULED, ReminderStatus.RETRYING}
)
