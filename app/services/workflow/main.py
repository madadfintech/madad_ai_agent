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
