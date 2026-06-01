"""Communication Service FastAPI app (Application Server container, port 8002).

Exposes the communication API:
* ``POST /comm/inbound``  — ingest a normalized inbound message (from MCP layer)
* ``POST /comm/outbound`` — send an outbound message (text or CMS template)
* ``POST /comm/delivery`` — apply a delivery-status callback
* ``GET  /comm/conversations/{id}/messages`` — conversation history (ops/replay)
* ``GET  /health``
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Annotated

from fastapi import Depends, FastAPI

from app.core.app import create_service_app
from app.shared.events import connect_forwarders, get_event_bus

from .deps import get_communication_service
from .schemas import (
    DeliveryStatusUpdate,
    InboundMessageDTO,
    MessageDTO,
    OutboundMessageRequest,
)
from .service import CommunicationService


@asynccontextmanager
async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
    # Forward this service's events onto the unified cross-process bus.
    connect_forwarders(get_event_bus(), communication=get_communication_service().events)
    yield


app = create_service_app(
    title="MADAD Communication Service",
    service="communication",
    api_auth=True,
    lifespan=lifespan,
)

Service = Annotated[CommunicationService, Depends(get_communication_service)]


@app.post("/comm/inbound", response_model=MessageDTO)
async def inbound(dto: InboundMessageDTO, service: Service) -> MessageDTO:
    message = await service.ingest_inbound(dto)
    return MessageDTO.from_model(message)


@app.post("/comm/outbound", response_model=MessageDTO)
async def outbound(request: OutboundMessageRequest, service: Service) -> MessageDTO:
    message = await service.send(request)
    return MessageDTO.from_model(message)


@app.post("/comm/delivery", response_model=MessageDTO)
async def delivery(update: DeliveryStatusUpdate, service: Service) -> MessageDTO:
    message = await service.update_delivery_status(update)
    return MessageDTO.from_model(message)


@app.get("/comm/conversations/{conversation_id}/messages", response_model=list[MessageDTO])
async def conversation_messages(conversation_id: str, service: Service) -> list[MessageDTO]:
    messages = await service.get_messages(conversation_id)
    return [MessageDTO.from_model(m) for m in messages]
