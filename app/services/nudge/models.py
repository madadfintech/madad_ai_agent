"""Nudge domain models.

A *schedule* (from CMS) defines the steps of a reminder sequence. A *sequence* is
a running instance for one target + reason, and each step it reaches becomes a
scheduled *reminder*. Only one reminder is pending per sequence at a time (lazy
scheduling), which keeps suppression simple and prevents duplicate nudges.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field

from app.shared.i18n import DEFAULT_LOCALE, Locale
from app.shared.workflow.enums import Channel
from app.shared.workflow.utils import new_id, utcnow

from .enums import ReminderStatus, SequenceStatus


class NudgeStep(BaseModel):
    """One step of a reminder schedule."""

    offset_seconds: int  # delay from the sequence's base time
    channels: list[Channel]
    template_key: str
    escalate: bool = False  # raise to ops after sending this step


class NudgeScheduleConfig(BaseModel):
    """A reminder schedule for a reason — sourced from CMS at runtime."""

    reason: str
    steps: list[NudgeStep]
    max_attempts: int = 3

    @classmethod
    def from_cms_value(cls, reason: str, value: dict[str, Any]) -> NudgeScheduleConfig:
        """Parse a CMS ``NUDGE`` config value (``{"schedule": [...]}``)."""

        steps = [
            NudgeStep(
                offset_seconds=int(item["offset"]),
                channels=[Channel(c) for c in item.get("channels", [])],
                template_key=str(item["template_key"]),
                escalate=bool(item.get("escalate", False)),
            )
            for item in value.get("schedule", [])
        ]
        return cls(
            reason=reason,
            steps=steps,
            max_attempts=int(value.get("max_attempts", 3)),
        )


class NudgeSequence(BaseModel):
    """A running reminder campaign for one target + reason."""

    sequence_id: str = Field(default_factory=lambda: new_id("nseq"))
    reason: str
    # Per-channel contact points for the same user (channel-is-identity).
    targets: dict[Channel, str] = Field(default_factory=dict)
    target_ref: str | None = None  # e.g. application ref / invoice id
    variables: dict[str, Any] = Field(default_factory=dict)
    locale: Locale = DEFAULT_LOCALE
    status: SequenceStatus = SequenceStatus.ACTIVE
    current_step: int = 0
    base_time: datetime = Field(default_factory=utcnow)
    correlation_id: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime = Field(default_factory=utcnow)
    updated_at: datetime = Field(default_factory=utcnow)


class Reminder(BaseModel):
    """A single scheduled reminder (one step of a sequence)."""

    reminder_id: str = Field(default_factory=lambda: new_id("nrem"))
    sequence_id: str
    step_index: int
    scheduled_for: datetime
    channels: list[Channel] = Field(default_factory=list)
    template_key: str
    escalate: bool = False
    status: ReminderStatus = ReminderStatus.SCHEDULED
    attempts: int = 0
    last_error: str | None = None
    created_at: datetime = Field(default_factory=utcnow)
    updated_at: datetime = Field(default_factory=utcnow)
    sent_at: datetime | None = None
