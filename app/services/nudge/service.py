"""Nudge & notification orchestration.

Responsibilities: start reminder sequences, schedule the next step (lazily, one
pending reminder per sequence), drain due reminders on each worker tick
(``run_due`` — driven by Celery beat in production), dispatch through the
Communication service with retry/backoff, suppress on completion, and escalate to
ops. Timings/channels/content come from CMS, so all of this is runtime-configurable.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from app.core.logging import get_logger
from app.shared.i18n import DEFAULT_LOCALE, Locale
from app.shared.workflow.context import Clock, SystemClock
from app.shared.workflow.enums import Channel
from app.shared.workflow.utils import compute_backoff

from .audit import NudgeAuditLogger
from .config_provider import NudgeConfigProvider
from .dispatcher import NotificationDispatcher
from .enums import SEQUENCE_TERMINAL, ReminderStatus, SequenceStatus
from .errors import SequenceNotFoundError
from .events import NudgeEvent, NudgeEventBus, NudgeEventType
from .models import NudgeScheduleConfig, NudgeSequence, Reminder
from .persistence import NudgeStore


@dataclass
class NudgeConfig:
    """Service-level retry tunables (schedule-level max_attempts comes from CMS)."""

    retry_base_delay: float = 30.0
    retry_max_delay: float = 3600.0
    retry_jitter: bool = True
    default_locale: Locale = DEFAULT_LOCALE


class NudgeService:
    """Reminder sequence orchestration."""

    def __init__(
        self,
        *,
        store: NudgeStore,
        config_provider: NudgeConfigProvider,
        dispatcher: NotificationDispatcher,
        events: NudgeEventBus,
        audit: NudgeAuditLogger,
        config: NudgeConfig | None = None,
        clock: Clock | None = None,
        logger: Any | None = None,
    ) -> None:
        self._store = store
        self._configs = config_provider
        self._dispatcher = dispatcher
        self._events = events
        self._audit = audit
        self._config = config or NudgeConfig()
        self._clock = clock or SystemClock()
        self._log = logger or get_logger("nudge.service")

    @property
    def events(self) -> NudgeEventBus:
        """The in-process event bus (for forwarding onto the unified bus)."""

        return self._events

    # -- starting sequences ---------------------------------------------------

    async def start_sequence(
        self,
        reason: str,
        targets: dict[Channel, str],
        *,
        variables: dict[str, Any] | None = None,
        target_ref: str | None = None,
        locale: Locale | None = None,
        base_time: datetime | None = None,
        correlation_id: str | None = None,
    ) -> NudgeSequence:
        """Start a reminder sequence. Idempotent per (reason, target_ref)."""

        if target_ref is not None:
            existing = await self._store.find_active_sequence(reason, target_ref)
            if existing is not None:
                return existing  # prevent duplicate nudges

        schedule = await self._configs.get_schedule(reason)
        sequence = NudgeSequence(
            reason=reason,
            targets=targets,
            target_ref=target_ref,
            variables=variables or {},
            locale=locale or self._config.default_locale,
            base_time=base_time or self._clock.now(),
            correlation_id=correlation_id,
        )
        await self._store.create_sequence(sequence)
        await self._emit(NudgeEventType.SEQUENCE_STARTED, sequence)
        await self._audit.record(
            sequence.sequence_id, "sequence_started", detail={"reason": reason}
        )
        await self._schedule_step(sequence, schedule, 0)
        return sequence

    # -- worker tick ----------------------------------------------------------

    async def run_due(self, now: datetime | None = None, *, limit: int = 100) -> list[Reminder]:
        """Drain and process all reminders due at ``now`` (the worker tick)."""

        now = now or self._clock.now()
        due = await self._store.list_due(now, limit=limit)
        processed: list[Reminder] = []
        for reminder in due:
            sequence = await self._store.get_sequence(reminder.sequence_id)
            if sequence is None or sequence.status in SEQUENCE_TERMINAL:
                reminder.status = ReminderStatus.SUPPRESSED
                await self._store.save_reminder(reminder)
                continue
            schedule = await self._configs.get_schedule(sequence.reason)
            await self._dispatch_reminder(reminder, sequence, schedule)
            processed.append(reminder)
        if processed:
            self._log.info("nudge.run_due", processed=len(processed))
        return processed

    # -- suppression / escalation / cancellation ------------------------------

    async def suppress(self, sequence_id: str, *, reason: str = "completed") -> NudgeSequence:
        """Stop reminding (the awaited action happened)."""

        sequence = await self._require_sequence(sequence_id)
        if sequence.status in SEQUENCE_TERMINAL:
            return sequence
        sequence.status = SequenceStatus.SUPPRESSED
        await self._store.save_sequence(sequence)
        await self._cancel_pending(sequence, ReminderStatus.SUPPRESSED)
        await self._emit(NudgeEventType.SEQUENCE_SUPPRESSED, sequence, payload={"reason": reason})
        await self._audit.record(sequence_id, "suppressed", detail={"reason": reason})
        return sequence

    async def suppress_matching(
        self,
        *,
        target_ref: str | None = None,
        identity: str | None = None,
        reason: str | None = None,
    ) -> list[NudgeSequence]:
        """Suppress all active sequences matching a target (event-driven path)."""

        sequences = await self._store.list_active_sequences(
            target_ref=target_ref, identity=identity
        )
        suppressed = []
        for sequence in sequences:
            if reason is not None and sequence.reason != reason:
                continue
            suppressed.append(await self.suppress(sequence.sequence_id))
        return suppressed

    async def cancel(self, sequence_id: str) -> NudgeSequence:
        sequence = await self._require_sequence(sequence_id)
        if sequence.status in SEQUENCE_TERMINAL:
            return sequence
        sequence.status = SequenceStatus.CANCELLED
        await self._store.save_sequence(sequence)
        await self._cancel_pending(sequence, ReminderStatus.CANCELLED)
        await self._emit(NudgeEventType.SEQUENCE_CANCELLED, sequence)
        await self._audit.record(sequence_id, "cancelled")
        return sequence

    # -- reads ----------------------------------------------------------------

    async def get_sequence(self, sequence_id: str) -> NudgeSequence:
        return await self._require_sequence(sequence_id)

    async def list_reminders(self, sequence_id: str) -> list[Reminder]:
        return await self._store.list_by_sequence(sequence_id)

    # -- internals ------------------------------------------------------------

    async def _schedule_step(
        self, sequence: NudgeSequence, schedule: NudgeScheduleConfig, step_index: int
    ) -> Reminder | None:
        if step_index >= len(schedule.steps):
            sequence.status = SequenceStatus.COMPLETED
            await self._store.save_sequence(sequence)
            await self._emit(NudgeEventType.SEQUENCE_COMPLETED, sequence)
            await self._audit.record(sequence.sequence_id, "sequence_completed")
            return None

        step = schedule.steps[step_index]
        reminder = Reminder(
            sequence_id=sequence.sequence_id,
            step_index=step_index,
            scheduled_for=sequence.base_time + timedelta(seconds=step.offset_seconds),
            channels=step.channels,
            template_key=step.template_key,
            escalate=step.escalate,
        )
        await self._store.create_reminder(reminder)
        sequence.current_step = step_index
        await self._store.save_sequence(sequence)
        await self._emit(
            NudgeEventType.REMINDER_SCHEDULED,
            sequence,
            reminder=reminder,
            payload={"scheduled_for": reminder.scheduled_for.isoformat()},
        )
        await self._audit.record(
            sequence.sequence_id,
            "reminder_scheduled",
            reminder_id=reminder.reminder_id,
            detail={"step": step_index},
        )
        return reminder

    async def _dispatch_reminder(
        self, reminder: Reminder, sequence: NudgeSequence, schedule: NudgeScheduleConfig
    ) -> None:
        errors: list[str] = []
        sent_any = False
        for channel in reminder.channels:
            identity = sequence.targets.get(channel)
            if not identity:
                continue  # no contact point for this channel
            try:
                await self._dispatcher.dispatch(
                    channel,
                    identity,
                    template_key=reminder.template_key,
                    variables=sequence.variables,
                    locale=sequence.locale,
                    correlation_id=sequence.correlation_id,
                )
                sent_any = True
            except Exception as exc:  # noqa: BLE001 - transient delivery failure
                errors.append(f"{channel}: {exc}")

        if errors and not sent_any:
            await self._handle_dispatch_failure(reminder, sequence, schedule, errors)
            return

        reminder.status = ReminderStatus.SENT
        reminder.sent_at = self._clock.now()
        reminder.last_error = None
        await self._store.save_reminder(reminder)
        await self._emit(NudgeEventType.REMINDER_SENT, sequence, reminder=reminder)
        await self._audit.record(
            sequence.sequence_id, "reminder_sent", reminder_id=reminder.reminder_id
        )

        if reminder.escalate:
            await self._escalate(sequence)
            return  # escalation is terminal; no further steps

        await self._schedule_step(sequence, schedule, reminder.step_index + 1)

    async def _handle_dispatch_failure(
        self,
        reminder: Reminder,
        sequence: NudgeSequence,
        schedule: NudgeScheduleConfig,
        errors: list[str],
    ) -> None:
        reminder.attempts += 1
        reminder.last_error = "; ".join(errors)
        if reminder.attempts < schedule.max_attempts:
            delay = compute_backoff(
                reminder.attempts,
                base_delay=self._config.retry_base_delay,
                max_delay=self._config.retry_max_delay,
                jitter=self._config.retry_jitter,
            )
            reminder.status = ReminderStatus.RETRYING
            reminder.scheduled_for = self._clock.now() + timedelta(seconds=delay)
            await self._store.save_reminder(reminder)
            await self._emit(
                NudgeEventType.REMINDER_RETRYING,
                sequence,
                reminder=reminder,
                payload={"attempt": reminder.attempts, "delay": delay},
            )
            await self._audit.record(
                sequence.sequence_id,
                "reminder_retrying",
                reminder_id=reminder.reminder_id,
                detail={"attempt": reminder.attempts, "error": reminder.last_error},
            )
            return

        reminder.status = ReminderStatus.FAILED
        await self._store.save_reminder(reminder)
        await self._emit(NudgeEventType.REMINDER_FAILED, sequence, reminder=reminder)
        await self._audit.record(
            sequence.sequence_id,
            "reminder_failed",
            reminder_id=reminder.reminder_id,
            detail={"error": reminder.last_error},
        )
        # A failed delivery shouldn't kill the sequence — proceed to the next step.
        await self._schedule_step(sequence, schedule, reminder.step_index + 1)

    async def _escalate(self, sequence: NudgeSequence) -> None:
        sequence.status = SequenceStatus.ESCALATED
        await self._store.save_sequence(sequence)
        await self._cancel_pending(sequence, ReminderStatus.CANCELLED)
        await self._emit(NudgeEventType.SEQUENCE_ESCALATED, sequence)
        await self._audit.record(sequence.sequence_id, "escalated")

    async def _cancel_pending(
        self, sequence: NudgeSequence, status: ReminderStatus
    ) -> None:
        for reminder in await self._store.list_pending_by_sequence(sequence.sequence_id):
            reminder.status = status
            await self._store.save_reminder(reminder)

    async def _require_sequence(self, sequence_id: str) -> NudgeSequence:
        sequence = await self._store.get_sequence(sequence_id)
        if sequence is None:
            raise SequenceNotFoundError(
                f"Nudge sequence {sequence_id!r} not found",
                details={"sequence_id": sequence_id},
            )
        return sequence

    async def _emit(
        self,
        event_type: NudgeEventType,
        sequence: NudgeSequence,
        *,
        reminder: Reminder | None = None,
        payload: dict[str, Any] | None = None,
    ) -> None:
        await self._events.publish(
            NudgeEvent(
                type=event_type,
                sequence_id=sequence.sequence_id,
                reminder_id=reminder.reminder_id if reminder else None,
                reason=sequence.reason,
                target_ref=sequence.target_ref,
                payload=payload or {},
            )
        )
