"""Nudge service exception hierarchy."""

from __future__ import annotations

from app.core.exceptions import AppError


class NudgeError(AppError):
    code = "nudge_error"


class ScheduleNotFoundError(NudgeError):
    """No reminder schedule is configured for a reason (in CMS)."""

    code = "nudge_schedule_not_found"
    http_status = 404


class SequenceNotFoundError(NudgeError):
    code = "nudge_sequence_not_found"
    http_status = 404


class NotificationDispatchError(NudgeError):
    """A transient failure delivering a notification (triggers retry)."""

    code = "nudge_dispatch_error"
