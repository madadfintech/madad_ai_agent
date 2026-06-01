"""Nudge & Notification Service FastAPI app (Application Server container, port 8004).

Orchestration/admin APIs. ``POST /nudge/run-due`` is the worker tick the Celery
beat scheduler invokes in production; it is exposed here for ops/cron triggering.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Annotated

from fastapi import Depends, FastAPI

from app.core.app import create_service_app
from app.shared.events import connect_forwarders, get_event_bus

from .deps import get_nudge_service
from .schemas import (
    ReminderDTO,
    SequenceDetailDTO,
    SequenceDTO,
    StartSequenceRequest,
    SuppressRequest,
)
from .service import NudgeService


@asynccontextmanager
async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
    connect_forwarders(get_event_bus(), nudge=get_nudge_service().events)
    yield


app = create_service_app(
    title="MADAD Nudge & Notification Service", service="nudge", lifespan=lifespan
)

Service = Annotated[NudgeService, Depends(get_nudge_service)]


@app.post("/nudge/sequences", response_model=SequenceDTO)
async def start_sequence(req: StartSequenceRequest, service: Service) -> SequenceDTO:
    sequence = await service.start_sequence(
        req.reason,
        req.targets,
        variables=req.variables,
        target_ref=req.target_ref,
        locale=req.locale,
        correlation_id=req.correlation_id,
    )
    return SequenceDTO.from_model(sequence)


@app.get("/nudge/sequences/{sequence_id}", response_model=SequenceDetailDTO)
async def get_sequence(sequence_id: str, service: Service) -> SequenceDetailDTO:
    sequence = await service.get_sequence(sequence_id)
    reminders = await service.list_reminders(sequence_id)
    base = SequenceDTO.from_model(sequence)
    return SequenceDetailDTO(
        **base.model_dump(),
        reminders=[ReminderDTO.from_model(r) for r in reminders],
    )


@app.post("/nudge/sequences/{sequence_id}/suppress", response_model=SequenceDTO)
async def suppress_sequence(sequence_id: str, service: Service) -> SequenceDTO:
    sequence = await service.suppress(sequence_id)
    return SequenceDTO.from_model(sequence)


@app.post("/nudge/sequences/{sequence_id}/cancel", response_model=SequenceDTO)
async def cancel_sequence(sequence_id: str, service: Service) -> SequenceDTO:
    sequence = await service.cancel(sequence_id)
    return SequenceDTO.from_model(sequence)


@app.post("/nudge/suppress", response_model=list[SequenceDTO])
async def suppress_matching(req: SuppressRequest, service: Service) -> list[SequenceDTO]:
    sequences = await service.suppress_matching(
        target_ref=req.target_ref, identity=req.identity, reason=req.reason
    )
    return [SequenceDTO.from_model(s) for s in sequences]


@app.post("/nudge/run-due")
async def run_due(service: Service) -> dict[str, int]:
    processed = await service.run_due()
    return {"processed": len(processed)}
