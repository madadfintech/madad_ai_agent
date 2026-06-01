"""Conversational Workflow Service FastAPI app (Application Server, port 8001).

Drives the onboarding workflow:
* ``POST /workflow/campaign/start`` — start onboarding (campaign entry / Step 0)
* ``POST /workflow/inbound``        — feed an inbound channel message (start/resume)
* ``POST /workflow/webhooks/{kind}``— resume on an external decision (pre-qual,
  score, payment, offers, offer_selection)
* ``GET  /workflow/status``         — current run status for a channel-identity
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Annotated, Any

from fastapi import Depends, FastAPI
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from app.core.app import create_service_app
from app.core.security import verify_webhook_signature
from app.shared.events import connect_forwarders, get_event_bus
from app.shared.workflow import Channel, ExecutionResult
from app.shared.workflow.errors import SessionNotFoundError

from .deps import OnboardingPlatform, get_onboarding_platform


@asynccontextmanager
async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
    platform = get_onboarding_platform()
    await platform.runtime.setup()  # e.g. provision the Postgres checkpointer
    connect_forwarders(get_event_bus(), workflow=platform.runtime.events)
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


class OfferAcceptanceRequest(BaseModel):
    """Offer acceptance from Madad's platform (the user selected an offer after
    being routed to madadfintech.com)."""

    channel: Channel
    identity: str
    offer_id: str | None = None


class MadadStatusRequest(BaseModel):
    """A backend status update pushed by Madad's core (NOT Tess/external).

    Carries the async financing decisions the agent is waiting on.
    """

    channel: Channel
    identity: str
    payload: dict[str, Any] = Field(default_factory=dict)


# Async decisions Madad's backend reports (Tess payment is confirmed by Madad,
# never received here directly).
_MADAD_STATUS_EVENTS = {"prequalification", "score", "offers", "payment"}


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


@app.post("/workflow/inbound", response_model=RunStatusDTO)
async def inbound(req: InboundRequest, platform: Platform) -> RunStatusDTO:
    result = await platform.dispatcher.inbound(
        req.channel, req.identity, text=req.text, attachments=req.attachments
    )
    return RunStatusDTO.from_result(result)


@app.post(
    "/workflow/webhooks/offer-acceptance",
    response_model=RunStatusDTO,
    dependencies=[Depends(verify_webhook_signature)],
)
async def offer_acceptance(req: OfferAcceptanceRequest, platform: Platform) -> RunStatusDTO:
    """THE webhook receiver — scoped to offer-acceptance events from Madad's
    platform after the user is routed back from the lender-offer selection."""

    payload = {"type": "offer_selection", "offer_id": req.offer_id}
    result = await platform.dispatcher.resume_external(req.channel, req.identity, payload)
    return RunStatusDTO.from_result(result)


@app.post(
    "/workflow/madad/status/{event}",
    response_model=RunStatusDTO,
    dependencies=[Depends(verify_webhook_signature)],
)
async def madad_status(event: str, req: MadadStatusRequest, platform: Platform) -> RunStatusDTO:
    """Backend status callback from Madad's core for the async financing
    decisions (pre-qualification, score, offers ready, payment confirmed)."""

    if event not in _MADAD_STATUS_EVENTS:
        return JSONResponse(  # type: ignore[return-value]
            status_code=400, content={"code": "unknown_status_event", "message": event}
        )
    payload = {"type": event, **req.payload}
    result = await platform.dispatcher.resume_external(req.channel, req.identity, payload)
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
