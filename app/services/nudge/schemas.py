"""DTOs for the nudge service API."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field

from app.shared.i18n import Locale
from app.shared.workflow.enums import Channel

from .enums import ReminderStatus, SequenceStatus
from .models import NudgeSequence, Reminder


class StartSequenceRequest(BaseModel):
    reason: str
    targets: dict[Channel, str]
    variables: dict[str, Any] = Field(default_factory=dict)
    target_ref: str | None = None
    locale: Locale | None = None
    correlation_id: str | None = None


class SuppressRequest(BaseModel):
    target_ref: str | None = None
    identity: str | None = None
    reason: str | None = None


class ReminderDTO(BaseModel):
    reminder_id: str
    step_index: int
    scheduled_for: datetime
    channels: list[Channel]
    template_key: str
    status: ReminderStatus
    attempts: int
    last_error: str | None

    @classmethod
    def from_model(cls, reminder: Reminder) -> ReminderDTO:
        return cls(
            reminder_id=reminder.reminder_id,
            step_index=reminder.step_index,
            scheduled_for=reminder.scheduled_for,
            channels=reminder.channels,
            template_key=reminder.template_key,
            status=reminder.status,
            attempts=reminder.attempts,
            last_error=reminder.last_error,
        )


class SequenceDTO(BaseModel):
    sequence_id: str
    reason: str
    targets: dict[Channel, str]
    target_ref: str | None
    status: SequenceStatus
    current_step: int
    locale: Locale

    @classmethod
    def from_model(cls, sequence: NudgeSequence) -> SequenceDTO:
        return cls(
            sequence_id=sequence.sequence_id,
            reason=sequence.reason,
            targets=sequence.targets,
            target_ref=sequence.target_ref,
            status=sequence.status,
            current_step=sequence.current_step,
            locale=sequence.locale,
        )


class SequenceDetailDTO(SequenceDTO):
    reminders: list[ReminderDTO] = Field(default_factory=list)
