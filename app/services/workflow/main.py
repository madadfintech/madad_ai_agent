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

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Annotated, Any

from fastapi import Depends, FastAPI, Header
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from app.core.app import create_service_app
from app.core.security import verify_webhook_signature
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
    # Cold-start warmup: when MCP is on, the first request to Cloud Run
    # takes 2-4 seconds for the instance to boot. Fire one cheap MCP call
    # here so the first real user-driven turn already sees a warm cluster.
    await _warmup_mcp()
    try:
        yield
    finally:
        await platform.runtime.aclose()


async def _warmup_mcp() -> None:
    """Best-effort cold-start warmup. Fires AUTH_CHECK_CONTACT (cheap,
    auth-free, no side effects) to wake the Cloud Run instance so the
    first user request is sub-second instead of 2-3 seconds. Silently
    no-ops when MCP is disabled or the call fails."""

    from app.core.config import settings as _s

    if not _s.mcp.enabled:
        return
    try:
        from app.shared.mcp import Tools, get_mcp_client

        mcp = get_mcp_client()
        await mcp.call_tool(Tools.AUTH_CHECK_CONTACT, {"email": "warmup@example.invalid"})
    except Exception:  # noqa: BLE001 — warmup is best-effort
        return


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
    result = await platform.runtime.start(
        "onboarding",
        req.channel,
        req.identity,
        input={"trigger": "campaign", "locale": req.locale},
    )
    return RunStatusDTO.from_result(result)


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
        outcome=None,
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


_OPS_SECRETS = frozenset({
    "access_token", "onboarding_token", "refresh_token",
    "token_expires_at",
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
    50, max 200. Auth: admin-bearer (gated by ``require_admin`` on the
    workflow service).
    """
    from app.shared.workflow import RunStatus

    runtime = platform.runtime
    if status:
        try:
            statuses = (RunStatus(status),)
        except ValueError as exc:
            raise UnknownEventTypeError(f"unknown status: {status}") from exc
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
    scrubbed = {
        k: v for k, v in raw_state.items() if k not in _OPS_SECRETS
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


@app.get(
    "/workflow/admin/ops/summary",
    response_model=OpsSummary,
)
async def ops_summary(platform: Platform) -> OpsSummary:
    """One-shot operational summary for the MADAD ops home page.

    Aggregates run counts by status + Phase 1.b invoice/disbursement
    totals from the runs' state ledgers. Auth: admin-bearer.
    """
    from app.shared.workflow import RunStatus

    runtime = platform.runtime
    all_runs = await runtime.run_store.list_by_status(*list(RunStatus))
    waiting = sum(1 for r in all_runs if str(r.status) == "waiting_for_input")
    running = sum(1 for r in all_runs if str(r.status) == "running")
    completed = sum(1 for r in all_runs if str(r.status) == "completed")
    failed = sum(1 for r in all_runs if str(r.status) == "failed")

    # Walk the recent active runs' state for Phase 1.b totals — bounded
    # at 200 most-recent so the endpoint stays O(1) per request.
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
