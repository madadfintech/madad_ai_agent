"""Journey-status polling worker.

Backstop for the webhook chokepoint. Many backend-status transitions for an
onboarding run come through Madad's webhooks (Phase 4 receivers); the poller
runs on a Celery beat schedule to catch the cases where a webhook is missed
or delayed.

Scan logic (one beat tick):

  1. Read every workflow run in ``WAITING_FOR_INPUT`` status from the run
     store. (The store already has indexes for this — no N+1.)
  2. Skip runs not parked at a polling-relevant step (``journey_wait_await``
     or ``lender_wait_await``). Inbound-await steps need user input; the
     poller can't advance them.
  3. For each remaining run, read the LangGraph state to get
     ``journey_status``, ``last_polled_at``, ``last_status_source``.
  4. Apply cadence by journey-status group (15 min for PRE_QUALIFIED /
     QUALIFIED; 5 min for ELIGIBLE; 1 h default).
  5. If a webhook was the most recent source AND less than one cadence-
     window has elapsed, skip this cycle (gives the webhook-driven poll a
     chance to settle before piling on another).
  6. If still due, resume the workflow with a synthetic
     ``{type: "status_update", last_status_source: "poll"}`` payload — the
     wait-await captures the source; ``status_poll_on_demand`` re-calls
     ``auth_me`` and routes on the fresh journey_status.

This module is async; the Celery beat task wraps it via ``asyncio.run``
(see :mod:`app.workers.tasks`).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from app.services.workflow.deps import OnboardingPlatform, get_onboarding_platform
from app.services.workflow.state import JourneyStatus
from app.shared.workflow.enums import RunStatus
from app.shared.workflow.persistence import WorkflowRun

# Polling step names — the only awaits this worker advances.
POLLABLE_STEPS: frozenset[str] = frozenset({"journey_wait_await", "lender_wait_await"})

# Document-upload await: the settle sweep resumes a run parked here once the SME
# has stopped uploading, so the workflow can send the end-of-batch checklist +
# tappable YES/NO button prompt (UAT 2026-06-13).
DOCS_UPLOAD_STEP = "documents_upload_loop_await"
# How long the upload stream must be quiet before we fire the settle prompt.
DOCS_SETTLE_QUIET = timedelta(seconds=25)

# Cadence per journey-status group, expressed as a timedelta.
CADENCE_OFFER = timedelta(seconds=60)   # ACCEPTED/OFFER_ACCEPTED — see below
CADENCE_FAST = timedelta(minutes=5)     # ELIGIBLE post-payment (close to ready)
CADENCE_MEDIUM = timedelta(minutes=15)  # PRE_QUALIFIED / QUALIFIED (lender phase)
CADENCE_SLOW = timedelta(hours=1)       # everything else (default backstop)


def cadence_for(status: JourneyStatus | str | None) -> timedelta:
    """Return the polling cadence to use for a given journey status."""

    if status in (JourneyStatus.PRE_QUALIFIED, JourneyStatus.QUALIFIED):
        return CADENCE_MEDIUM
    if status == JourneyStatus.ELIGIBLE:
        return CADENCE_FAST
    # Offer phase: the SME selects an offer / the line is activated on the
    # PORTAL, and the backend's offer.selected webhook can't reach us (it's not
    # allow-listed there). Poll fast (≈1 min, matching the beat tick) so the ✅
    # "offer selected" confirmation and 🎊 activation fire promptly off /me's
    # journey_status instead of waiting up to an hour. Short-lived phase, so the
    # extra auth_me calls are bounded.
    if status in (JourneyStatus.ACCEPTED, JourneyStatus.OFFER_ACCEPTED):
        return CADENCE_OFFER
    return CADENCE_SLOW


def poll_due(
    *,
    last_polled_at: datetime | None,
    last_status_source: str | None,
    now: datetime,
    cadence: timedelta,
) -> bool:
    """Decide whether a run is due for a poll right now.

    * Never polled → always due.
    * Less than one cadence-window since the last poll → not due.
    * Webhook was the most recent source AND less than two cadence-windows
      have elapsed → skip ONE cycle (the cadence after the webhook).
    * Otherwise → due.
    """

    if last_polled_at is None:
        return True
    elapsed = now - last_polled_at
    if elapsed < cadence:
        return False
    if last_status_source == "webhook" and elapsed < cadence * 2:
        return False
    return True


@dataclass
class PollerStats:
    """One beat-tick's worth of work."""

    polled: int = 0
    skipped_step: int = 0  # not at a polling-relevant await
    skipped_cadence: int = 0  # at a polling await but not due
    failed: int = 0


async def _read_state(
    platform: OnboardingPlatform, run: WorkflowRun
) -> dict[str, Any] | None:
    """Pull the LangGraph state values for a run. Returns None if the
    checkpoint is unavailable (newly created run, or non-checkpointable)."""

    compiled = platform.runtime.loader.load(run.workflow, run.version)
    config = {"configurable": {"thread_id": run.thread_id}}
    snap = await compiled.graph.aget_state(config)
    values = snap.values if snap is not None else None
    if not isinstance(values, dict):
        return None
    return values


async def _maybe_settle_docs(
    platform: OnboardingPlatform, run: WorkflowRun, now: datetime
) -> bool:
    """Resume a docs-upload run with a synthetic ``docs_settle`` event once the
    SME has stopped uploading for ``DOCS_SETTLE_QUIET`` and required docs are
    still missing — the cue for the workflow to send the end-of-batch checklist
    + tappable YES/NO button prompt exactly once. Returns True if resumed."""

    if run.channel is None or not run.identity:
        return False
    values = await _read_state(platform, run)
    if not values:
        return False
    missing = values.get("missing_documents") or []
    if not missing:
        return False  # checklist complete → the coffee/complete path handles it
    if values.get("docs_settle_prompted"):
        return False  # already prompted for this quiet period
    last_upload = values.get("docs_last_upload_at")
    if isinstance(last_upload, str):
        last_upload = _parse_iso(last_upload)
    if last_upload is None or (now - last_upload) < DOCS_SETTLE_QUIET:
        return False  # still actively uploading — wait for quiet
    await platform.dispatcher.resume_external(
        run.channel,
        run.identity,
        {"type": "docs_settle"},
    )
    return True


async def run_status_poller(
    platform: OnboardingPlatform | None = None, *, now: datetime | None = None
) -> PollerStats:
    """Iterate every waiting run on the platform and poll the due ones.

    Returns a :class:`PollerStats` with per-bucket counts so the celery
    task wrapper can surface them to Prometheus / logs.
    """

    platform = platform or get_onboarding_platform()
    now = now or datetime.now(UTC)
    stats = PollerStats()

    waiting = await platform.runtime.run_store.list_by_status(
        RunStatus.WAITING_FOR_INPUT
    )
    for run in waiting:
        if run.current_step == DOCS_UPLOAD_STEP:
            # End-of-upload settle: resume the run so the workflow sends the
            # checklist + tappable button prompt once uploads go quiet.
            try:
                if await _maybe_settle_docs(platform, run, now):
                    stats.polled += 1
            except Exception:  # noqa: BLE001 — one bad run shouldn't kill the tick
                stats.failed += 1
            continue
        if run.current_step not in POLLABLE_STEPS:
            stats.skipped_step += 1
            continue
        try:
            values = await _read_state(platform, run)
            if values is None:
                stats.failed += 1
                continue
            journey_status = values.get("journey_status")
            last_polled_at = values.get("last_polled_at")
            last_status_source = values.get("last_status_source")
            if isinstance(last_polled_at, str):
                last_polled_at = _parse_iso(last_polled_at)
            cadence = cadence_for(journey_status)
            if not poll_due(
                last_polled_at=last_polled_at,
                last_status_source=last_status_source,
                now=now,
                cadence=cadence,
            ):
                stats.skipped_cadence += 1
                continue
            if run.channel is None or not run.identity:
                stats.failed += 1
                continue
            await platform.dispatcher.resume_external(
                run.channel,
                run.identity,
                {"type": "status_update", "last_status_source": "poll"},
            )
            stats.polled += 1
        except Exception:  # noqa: BLE001 - one bad run shouldn't kill the tick
            stats.failed += 1
    return stats


def _parse_iso(value: str) -> datetime | None:
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None
