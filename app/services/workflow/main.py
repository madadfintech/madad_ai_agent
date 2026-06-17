"""Conversational Workflow Service FastAPI app (Application Server, port 8001).

Drives the onboarding workflow:
* ``POST /workflow/campaign/start`` — start onboarding (campaign entry / Step 0)
* ``POST /workflow/inbound``        — feed an inbound channel message
  (start / resume on user reply).
* ``POST /workflow/madad/events/{event_type}`` — backend webhook chokepoint.
  Accepts any of the 14 canonical event types (8 Phase 1.a + 6 Phase 1.b,
  see :data:`dispatcher.ALL_BACKEND_EVENTS`). HMAC-verified;
  ``event_id`` (from ``X-Madad-Event-Id`` header or body) is deduped through
  the dispatcher's :class:`WebhookDedupe`.
* ``GET  /workflow/status``         — current run status for a channel-identity.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Annotated, Any

from fastapi import Depends, FastAPI, Header, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from app.core.app import create_service_app
from app.core.security import require_admin, verify_webhook_signature
from app.shared.events import connect_forwarders, get_event_bus
from app.shared.workflow import Channel, ExecutionResult
from app.shared.workflow.errors import SessionNotFoundError

from .deps import OnboardingPlatform, get_onboarding_platform
from .dispatcher import UnknownEventTypeError


@asynccontextmanager
async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
    platform = get_onboarding_platform()
    await platform.runtime.setup()  # e.g. provision the Postgres checkpointer
    connect_forwarders(get_event_bus(), workflow=platform.runtime.events)
    # The previous startup hook fired AUTH_CHECK_CONTACT against the MCP
    # cluster to warm Cloud Run, which produced a synthetic check_contact
    # audit-log entry on every restart. Cold-start mitigation is now
    # handled out-of-band (external uptime ping against /health) so we
    # don't pollute backend audit logs from inside the agent.
    try:
        yield
    finally:
        await platform.runtime.aclose()


app = create_service_app(
    title="MADAD Conversational Workflow Service",
    service="workflow",
    api_auth=True,
    lifespan=lifespan,
)

Platform = Annotated[OnboardingPlatform, Depends(get_onboarding_platform)]


class CampaignStartRequest(BaseModel):
    channel: Channel
    identity: str
    locale: str = "en"


class InboundRequest(BaseModel):
    channel: Channel
    identity: str
    text: str | None = None
    attachments: list[dict[str, Any]] = Field(default_factory=list)
    # Structured user-input payload — used when the active step expects a
    # form-shaped reply (eligibility intake, buyers / shareholders capture)
    # rather than free text or attachments. The dispatcher merges this into
    # the resume payload as top-level keys.
    data: dict[str, Any] = Field(default_factory=dict)
    # Source bridge's unique identifier for the inbound message (Meta wamid,
    # SendGrid Message-ID, etc.). When supplied, the dispatcher dedupes the
    # call so a retried webhook from the bridge does not re-play the same
    # user reply through the workflow. Optional — bridges that prefer to
    # supply it as the ``X-Madad-Message-Id`` header may also do so; the
    # header wins when both are present.
    message_id: str | None = None


class BackendEventRequest(BaseModel):
    """A backend webhook event pushed by Madad's core.

    Carries the channel-identity to address (the workflow is keyed on this
    pair), the optional ``event_id`` the dispatcher dedupes on, and the raw
    event ``payload`` whose shape is event-specific. The ``event_type`` is
    a URL path segment so logs and metrics can group by event.
    """

    channel: Channel
    identity: str
    event_id: str | None = None
    payload: dict[str, Any] = Field(default_factory=dict)


class RunStatusDTO(BaseModel):
    run_id: str
    status: str
    waiting: bool
    completed: bool
    current_step: str | None = None
    prompt: dict[str, Any] | None = None
    outcome: str | None = None

    @classmethod
    def from_result(cls, result: ExecutionResult) -> RunStatusDTO:
        return cls(
            run_id=result.run.run_id,
            status=str(result.status),
            waiting=result.waiting,
            completed=result.completed,
            current_step=result.run.current_step,
            prompt=result.prompt,
            outcome=result.values.get("outcome"),
        )


@app.post("/workflow/campaign/start", response_model=RunStatusDTO)
async def start_campaign(req: CampaignStartRequest, platform: Platform) -> RunStatusDTO:
    """Start a fresh onboarding campaign for (channel, identity).

    UAT 2026-06-17 RCA on +919497191690 — duplicate "Welcome back"
    messages tracked to TWO/THREE parallel ``campaign/start`` calls
    landing for the same number within seconds (the Madad bridge / WA
    broadcast retry pattern). The earlier ``_cancel_active_runs_for``
    did a SELECT-then-UPDATE which LOSES the race against the in-flight
    run's own state save — the predecessor "cancel" got overwritten
    and a second run ended up live. That second run independently went
    through ``entry_registration_check`` → registered → resume → fell
    through to ``_registered_route_send`` (the "Welcome back" terminal)
    and SHIPPED a duplicate message to the SME.

    Fix: ATOMIC idempotency lock via the same Redis ``SET NX EX``
    primitive the webhook dedupe uses. A second campaign/start within
    the lock window for the SAME ``(channel, identity)`` is absorbed:
    it returns the existing active run's status WITHOUT creating a
    parallel run. After the lock TTL expires, a legitimate user-
    initiated restart still cancels and creates a new run.

    UAT 2026-06-16 (prior fix retained): cancel predecessors before
    starting a new run, so a true restart still flushes the old session.
    """
    # ATOMIC dedupe: 30s lock keyed on (channel, identity). Reuses the
    # Webhook dedupe primitive (``SET NX EX``) we already deploy.
    lock_key = f"campaign_start:{str(getattr(req.channel, 'value', req.channel)).lower()}:{req.identity}"
    # Reuse the dispatcher's existing dedupe primitive (Redis SET NX EX
    # in prod / in-memory in tests). It's the same client the webhook
    # dedupe uses, so no new dependency.
    dedupe = platform.dispatcher._dedupe  # noqa: SLF001
    claimed = await dedupe.claim(lock_key, ttl_seconds=30)
    if not claimed:
        # Duplicate within the dedupe window — return the existing run's
        # status instead of spinning up a parallel one. The session's
        # active_run_id is the source of truth for subsequent inbounds.
        from app.core.logging import get_logger
        existing_run = await _find_active_run(
            platform, req.channel, req.identity,
        )
        if existing_run is not None:
            get_logger("workflow.campaign").info(
                "campaign_start.deduped",
                identity=req.identity,
                channel=str(req.channel),
                existing_run=existing_run.run_id,
            )
            return RunStatusDTO(
                run_id=existing_run.run_id,
                status=str(existing_run.status),
                waiting=str(existing_run.status) == "waiting_for_input",
                completed=str(existing_run.status) == "completed",
                current_step=getattr(existing_run, "current_step", None),
                prompt=None,
                outcome=None,
            )
        # Lock present but no active run — race window, treat as a
        # fresh start by clearing the dedupe assertion below.
        get_logger("workflow.campaign").warning(
            "campaign_start.deduped_no_existing_run",
            identity=req.identity,
            channel=str(req.channel),
            note="lock claimed but no active run found; proceeding as fresh",
        )

    cancelled_runs = await _cancel_active_runs_for(
        platform, req.channel, req.identity,
    )
    result = await platform.runtime.start(
        "onboarding",
        req.channel,
        req.identity,
        input={"trigger": "campaign", "locale": req.locale},
    )
    if cancelled_runs:
        # Log so ops can see when retriggers were absorbed into a clean
        # new run — helps explain "wait, where did my old run go?".
        from app.core.logging import get_logger
        get_logger("workflow.campaign").info(
            "campaign_start.cancelled_predecessors",
            new_run=result.run.run_id,
            cancelled=cancelled_runs,
        )
    return RunStatusDTO.from_result(result)


async def _find_active_run(
    platform: OnboardingPlatform, channel: Channel, identity: str,
) -> Any | None:
    """Return the most recently updated in-flight run for ``(channel, identity)``,
    or None when there are none. Mirrors ``_cancel_active_runs_for``'s match logic."""
    from app.shared.workflow import RunStatus

    waiting = await platform.runtime.run_store.list_by_status(
        RunStatus.WAITING_FOR_INPUT, RunStatus.RUNNING, RunStatus.PENDING,
    )
    ch_str = str(getattr(channel, "value", channel)).lower()
    matches = [
        run
        for run in waiting
        if str(getattr(run, "channel", "") or "").lower() == ch_str
        and (getattr(run, "identity", "") or "") == identity
    ]
    if not matches:
        return None
    # Most recently updated wins — the latest active run is the one the
    # session's active_run_id points at.
    matches.sort(key=lambda r: getattr(r, "updated_at", ""), reverse=True)
    return matches[0]


async def _cancel_active_runs_for(
    platform: OnboardingPlatform, channel: Channel, identity: str,
) -> list[str]:
    """Cancel every still-waiting workflow run for ``(channel, identity)``.

    Returns the list of cancelled run_ids. Idempotent — no-op when
    there are no live runs for the identity. Touches the run-store
    directly (cheap status flip), not the LangGraph checkpoint, so
    the next /workflow/admin/forget-session call still cleans those
    up if a deeper wipe is needed.
    """
    from app.shared.workflow import RunStatus

    cancelled: list[str] = []
    waiting = await platform.runtime.run_store.list_by_status(
        RunStatus.WAITING_FOR_INPUT, RunStatus.RUNNING, RunStatus.PENDING,
    )
    ch_str = str(getattr(channel, "value", channel)).lower()
    for run in waiting:
        run_ch = str(getattr(run, "channel", "") or "").lower()
        run_id_str = getattr(run, "identity", "") or ""
        if run_ch == ch_str and run_id_str == identity:
            run.status = RunStatus.CANCELLED
            await platform.runtime.run_store.save(run)
            cancelled.append(run.run_id)
    return cancelled


@app.post("/workflow/inbound", response_model=None)
async def inbound(
    req: InboundRequest,
    platform: Platform,
    x_madad_message_id: Annotated[str | None, Header()] = None,
) -> RunStatusDTO | JSONResponse:
    """Normalized inbound message chokepoint.

    The source bridge (Madad's Meta-WhatsApp / SendGrid inbound adapters)
    posts here with the channel-identity tuple plus any text/attachments
    the user sent. When a ``message_id`` is supplied (header wins over body
    field), the call is deduped — duplicate posts return 200 with
    ``{"deduped": true}`` so the source bridge does not keep retrying.
    """

    message_id = x_madad_message_id or req.message_id
    result = await platform.dispatcher.inbound(
        req.channel,
        req.identity,
        text=req.text,
        attachments=req.attachments,
        data=req.data or None,
        message_id=message_id,
    )
    if result is None:
        return JSONResponse(status_code=200, content={"deduped": True})
    return RunStatusDTO.from_result(result)


def _canon_event_identity(channel: Any, identity: str) -> str:
    """Canonicalise a backend webhook identity to the same E.164 form the inbound
    bridge keys sessions by, so the run still resolves when the backend sends the
    phone in a different shape (e.g. Qatar local "66563022" vs the session's
    "+97466563022"). WhatsApp only; other channels pass through untouched."""
    try:
        ch = str(getattr(channel, "value", channel)).lower()
    except Exception:  # noqa: BLE001
        ch = ""
    if ch != "whatsapp" or not identity:
        return identity
    digits = "".join(c for c in identity if c.isdigit())
    if not digits:
        return identity
    if len(digits) == 8:  # Qatar local number -> prepend country code
        digits = "974" + digits
    return "+" + digits


@app.post(
    "/workflow/madad/events/{event_type}",
    response_model=None,
    dependencies=[Depends(verify_webhook_signature)],
)
async def madad_event(
    event_type: str,
    req: BackendEventRequest,
    platform: Platform,
    x_madad_event_id: Annotated[str | None, Header()] = None,
) -> RunStatusDTO | JSONResponse:
    """Backend webhook chokepoint — accepts any of the 14 canonical event
    types defined in :data:`dispatcher.ALL_BACKEND_EVENTS`.

    Dedupe key resolves to ``X-Madad-Event-Id`` header first, falling back to
    ``event_id`` in the body (lets backend choose either transport).
    Duplicate posts return 200 with ``{"deduped": true}`` so the backend
    doesn't retry. Unknown event types return 400.
    """

    event_id = x_madad_event_id or req.event_id
    try:
        result = await platform.dispatcher.on_backend_event(
            event_type=event_type,
            event_id=event_id,
            channel=req.channel,
            identity=_canon_event_identity(req.channel, req.identity),
            payload=req.payload,
        )
    except UnknownEventTypeError as exc:
        return JSONResponse(
            status_code=400,
            content={"code": "unknown_event_type", "message": exc.event_type},
        )
    if result is None:
        return JSONResponse(status_code=200, content={"deduped": True})
    return RunStatusDTO.from_result(result)


class ForgetSessionRequest(BaseModel):
    channel: Channel
    identity: str


class ForgetSessionResponse(BaseModel):
    session_cleared: bool
    deleted_runs: int
    deleted_checkpoint_threads: int
    thread_ids: list[str] = Field(default_factory=list)


@app.post(
    "/workflow/admin/forget-session",
    response_model=ForgetSessionResponse,
    dependencies=[Depends(require_admin)],
)
async def forget_session(
    req: ForgetSessionRequest, platform: Platform
) -> ForgetSessionResponse:
    """Clear all conversation state for a channel-identity.

    Per Ishan's handover §10 (2026-06-09): the agent owns three stores
    (``workflow.runs`` + LangGraph checkpoints + Redis session). Backend
    DB wipes alone leave a parked run resuming stale, so this one-call
    reset is required for clean QA re-runs.

    Admin-token auth (the bearer used for every workflow API). Idempotent
    — returns zeros when no state existed for the identity.
    """

    runtime = platform.runtime
    from app.shared.workflow.utils import derive_session_id

    session_id = derive_session_id(req.channel, req.identity)
    thread_ids = await runtime.run_store.delete_by_session(session_id)

    saver = runtime.checkpointer.get()
    cleared_threads = 0
    for tid in thread_ids:
        try:
            await saver.adelete_thread(tid)
            cleared_threads += 1
        except Exception:  # noqa: BLE001 — best-effort, runs already gone
            continue

    session_existed = await runtime.sessions.forget(req.channel, req.identity)
    return ForgetSessionResponse(
        session_cleared=session_existed,
        deleted_runs=len(thread_ids),
        deleted_checkpoint_threads=cleared_threads,
        thread_ids=thread_ids,
    )


@app.get("/workflow/status", response_model=RunStatusDTO)
async def status(channel: Channel, identity: str, platform: Platform) -> RunStatusDTO:
    session = await platform.runtime.sessions.get(channel, identity)
    if session is None or not session.active_run_id:
        raise SessionNotFoundError(f"No active onboarding run for {channel.value}:{identity}")
    run = await platform.runtime.run_store.get(session.active_run_id)
    return RunStatusDTO(
        run_id=run.run_id,
        status=str(run.status),
        waiting=run.status.value == "waiting_for_input",
        completed=run.status.value == "completed",
        current_step=run.current_step,
        prompt=run.pending_interrupts[-1] if run.pending_interrupts else None,
        outcome=(run.metadata or {}).get("outcome"),
    )


# --------------------------------------------------------------------------
# Admin Ops API — surfaced to MADAD backend for operator visibility.
# Read-only run inspection + a paginated list. Reset is the existing
# /workflow/admin/forget-session endpoint.
# --------------------------------------------------------------------------

class OpsRunSummary(BaseModel):
    """Lean run summary for the Ops list page (one row per run)."""

    run_id: str
    workflow: str
    status: str
    channel: str | None = None
    identity: str | None = None
    current_step: str | None = None
    created_at: str | None = None
    updated_at: str | None = None
    completed_at: str | None = None


class OpsRunListResponse(BaseModel):
    runs: list[OpsRunSummary]
    total: int
    limit: int
    offset: int


class OpsRunDetail(BaseModel):
    """Full run view: summary + state values (with secrets stripped) +
    pending interrupts so the operator can see what the SME is waiting on."""

    run_id: str
    workflow: str
    status: str
    channel: str | None
    identity: str | None
    current_step: str | None
    created_at: str | None
    updated_at: str | None
    completed_at: str | None
    pending_interrupts: list[dict[str, Any]] = Field(default_factory=list)
    state: dict[str, Any] = Field(default_factory=dict)


class OpsSummary(BaseModel):
    """High-level ops summary — what every operator sees on the home page."""

    runs_total: int
    runs_waiting: int
    runs_running: int
    runs_completed: int
    runs_failed: int
    invoices_submitted: int
    disbursements_received: int


# Allow-list of state fields that are safe to surface via /ops/runs/{id}.
# We invert the previous block-list so a new OnboardingState field is hidden
# by default — adding KYC PII, document blobs, or payment links to the state
# never accidentally leaks them through the ops API. Operators who need a
# field added must do so explicitly.
#
# Excluded by design: any *_content_base64 (raw KYC PDFs), *_filename,
# *_mime_type, business_email + onboarding_first_name/last_name/cr_number/
# phone_override (PII), payment_link (live signed URL), idempotency_keys
# (payment dedup keys), eligibility_form_data + buyers + shareholders
# (PII bundles), check_contact_result / registration_payload (raw backend
# bodies that may carry PII).
_OPS_VISIBLE_STATE_FIELDS = frozenset({
    # identity / routing
    "locale", "channel_identity", "session_type",
    "registration_route",
    "domain_block_reason",
    "madad_user_id", "application_ref",
    # journey
    "journey_status", "last_polled_at", "last_status_source",
    "account_has_email",
    # progress flags (non-PII booleans / counts only)
    "consent", "cr_verified",
    "eligible", "financials_received",
    "documents_received", "docs_uploaded_count",
    "documents_complete_sent", "docs_proceed",
    "docs_settle_prompted",
    "prequalified", "payment_ready", "madad_score",
    "onboarding_progress_step",
    "paid", "offer_confirmed_sent", "offers_shown_sig",
    "outcome",
    # payment (amount + status + product id only — NO payment_link)
    "business_details_id", "payment_product_id",
    "payment_amount_qar", "payment_id", "payment_status",
    # missing-docs LIST (codes only — no contents)
    "missing_documents", "docs_acked",
    # Phase 1.b ledger summaries (sizes, not raw content)
    "repayment_outstanding_qar",
    # The lists below carry only the agent's local summaries (no base64,
    # no PII) so they're safe; per-record fields are already vetted by
    # the renderers in onboarding.py.
    "invoices_submitted", "disbursements_received", "repayments_recorded",
    "business_email_status",
    # debounce timestamps
    "documents_processing_ack_at", "documents_checklist_sent_at",
    "more_docs_prompt_at", "docs_last_upload_at",
})


def _to_summary(run: Any) -> OpsRunSummary:
    return OpsRunSummary(
        run_id=run.run_id,
        workflow=getattr(run, "workflow", "onboarding"),
        status=str(run.status),
        channel=getattr(run, "channel", None) and str(run.channel),
        identity=getattr(run, "identity", None),
        current_step=getattr(run, "current_step", None),
        created_at=str(getattr(run, "created_at", "") or "") or None,
        updated_at=str(getattr(run, "updated_at", "") or "") or None,
        completed_at=str(getattr(run, "completed_at", "") or "") or None,
    )


@app.get(
    "/workflow/admin/ops/runs",
    response_model=OpsRunListResponse,
    dependencies=[Depends(require_admin)],
)
async def ops_list_runs(
    platform: Platform,
    status: str | None = None,
    channel: str | None = None,
    limit: int = 50,
    offset: int = 0,
) -> OpsRunListResponse:
    """Paginated list of workflow runs for the MADAD ops portal.

    Filters by ``status`` (e.g. ``waiting_for_input`` / ``completed``)
    and ``channel`` (e.g. ``whatsapp`` / ``email``). Default page size
    50, max 200. Auth: admin-bearer.
    """
    from app.shared.workflow import RunStatus

    runtime = platform.runtime
    if status:
        try:
            statuses = (RunStatus(status),)
        except ValueError as exc:
            raise HTTPException(
                status_code=400,
                detail=f"unknown status: {status!r}",
            ) from exc
        runs = await runtime.run_store.list_by_status(*statuses)
    else:
        # No status filter: union the meaningful statuses. List_by_status
        # supports varargs so one call covers it.
        runs = await runtime.run_store.list_by_status(*list(RunStatus))
    if channel:
        ch = channel.lower()
        runs = [
            r for r in runs
            if str(getattr(r, "channel", "") or "").lower() == ch
        ]
    runs.sort(
        key=lambda r: str(getattr(r, "updated_at", "") or ""), reverse=True,
    )
    page = runs[offset : offset + min(limit, 200)]
    return OpsRunListResponse(
        runs=[_to_summary(r) for r in page],
        total=len(runs),
        limit=min(limit, 200),
        offset=offset,
    )


@app.get(
    "/workflow/admin/ops/runs/{run_id}",
    response_model=OpsRunDetail,
    dependencies=[Depends(require_admin)],
)
async def ops_get_run(run_id: str, platform: Platform) -> OpsRunDetail:
    """Full run detail — summary + scrubbed state values + pending
    interrupts. Auth: admin-bearer."""

    runtime = platform.runtime
    run = await runtime.run_store.get_or_none(run_id)
    if run is None:
        raise SessionNotFoundError(f"run not found: {run_id}")
    compiled = runtime.loader.load(run.workflow, run.version)
    try:
        snap = await compiled.graph.aget_state(
            {"configurable": {"thread_id": run.thread_id}}
        )
        raw_state: dict[str, Any] = dict(snap.values or {})
    except Exception:  # noqa: BLE001 — snapshot may be missing
        raw_state = {}
    # Allow-list (default-deny): a new state field is hidden until it's
    # explicitly added to ``_OPS_VISIBLE_STATE_FIELDS``.
    scrubbed = {
        k: v for k, v in raw_state.items() if k in _OPS_VISIBLE_STATE_FIELDS
    }
    return OpsRunDetail(
        run_id=run.run_id,
        workflow=run.workflow,
        status=str(run.status),
        channel=str(run.channel) if getattr(run, "channel", None) else None,
        identity=getattr(run, "identity", None),
        current_step=run.current_step,
        created_at=str(getattr(run, "created_at", "") or "") or None,
        updated_at=str(getattr(run, "updated_at", "") or "") or None,
        completed_at=str(getattr(run, "completed_at", "") or "") or None,
        pending_interrupts=list(run.pending_interrupts or []),
        state=scrubbed,
    )


# 30-second in-process cache for /ops/summary. The endpoint walks up to
# 200 LangGraph state snapshots per call to aggregate Phase 1.b totals;
# at a typical dashboard poll rate (15s × N viewers) that's a lot of
# Postgres round-trips for a number that doesn't need to be live to the
# second. We re-compute at most once every OPS_SUMMARY_CACHE_TTL_SECONDS
# and serve cached otherwise. Reset when the workflow process restarts.
OPS_SUMMARY_CACHE_TTL_SECONDS = 30.0
_ops_summary_cached_value: OpsSummary | None = None
_ops_summary_cached_at: float = 0.0
_ops_summary_lock = asyncio.Lock()


def reset_ops_summary_cache() -> None:
    """Invalidate the /ops/summary cache so the next call recomputes.

    Used by tests that drive state through the workflow then read summary
    counts; in production we just let the TTL expire.
    """
    global _ops_summary_cached_value, _ops_summary_cached_at
    _ops_summary_cached_value = None
    _ops_summary_cached_at = 0.0


async def _compute_ops_summary(platform: OnboardingPlatform) -> OpsSummary:
    from app.shared.workflow import RunStatus

    runtime = platform.runtime
    all_runs = await runtime.run_store.list_by_status(*list(RunStatus))
    waiting = sum(1 for r in all_runs if str(r.status) == "waiting_for_input")
    running = sum(1 for r in all_runs if str(r.status) == "running")
    completed = sum(1 for r in all_runs if str(r.status) == "completed")
    failed = sum(1 for r in all_runs if str(r.status) == "failed")

    # Walk only the 200 most-recent runs so a long-running deployment
    # doesn't make /summary slower over time. The cache then absorbs
    # repeat requests.
    recent = sorted(
        all_runs,
        key=lambda r: str(getattr(r, "updated_at", "") or ""),
        reverse=True,
    )[:200]
    invoices_submitted = 0
    disbursements_received = 0
    for r in recent:
        try:
            compiled = runtime.loader.load(r.workflow, r.version)
            snap = await compiled.graph.aget_state(
                {"configurable": {"thread_id": r.thread_id}}
            )
            state = snap.values or {}
            invoices_submitted += len(state.get("invoices_submitted") or [])
            disbursements_received += len(state.get("disbursements_received") or [])
        except Exception:  # noqa: BLE001 — degrade per-run
            continue

    return OpsSummary(
        runs_total=len(all_runs),
        runs_waiting=waiting,
        runs_running=running,
        runs_completed=completed,
        runs_failed=failed,
        invoices_submitted=invoices_submitted,
        disbursements_received=disbursements_received,
    )


@app.get(
    "/workflow/admin/ops/summary",
    response_model=OpsSummary,
    dependencies=[Depends(require_admin)],
)
async def ops_summary(platform: Platform) -> OpsSummary:
    """One-shot operational summary for the MADAD ops home page.

    Aggregates run counts by status + Phase 1.b invoice/disbursement
    totals from the runs' state ledgers. Auth: admin-bearer.
    Cached for OPS_SUMMARY_CACHE_TTL_SECONDS — operators see counts that
    may lag by up to that window, which is fine for a dashboard.
    """
    global _ops_summary_cached_value, _ops_summary_cached_at
    loop = asyncio.get_running_loop()
    now = loop.time()
    if (
        _ops_summary_cached_value is not None
        and (now - _ops_summary_cached_at) < OPS_SUMMARY_CACHE_TTL_SECONDS
    ):
        return _ops_summary_cached_value
    async with _ops_summary_lock:
        # Double-check inside the lock — another caller may have refreshed
        # the cache while we were waiting on the lock.
        now = loop.time()
        if (
            _ops_summary_cached_value is not None
            and (now - _ops_summary_cached_at) < OPS_SUMMARY_CACHE_TTL_SECONDS
        ):
            return _ops_summary_cached_value
        value = await _compute_ops_summary(platform)
        _ops_summary_cached_value = value
        _ops_summary_cached_at = loop.time()
        return value
