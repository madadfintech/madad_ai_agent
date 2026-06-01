"""CMS & Configuration Service FastAPI app (Application Server container, port 8003).

Backend admin APIs only (no UI). MADAD operators drive these to update templates,
checklists, workflow configs, nudge timings, and runtime variables — changes take
effect in live conversations within the cache TTL / on the next invalidation.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Annotated

from fastapi import Depends, FastAPI, Query

from app.core.app import create_service_app
from app.shared.events import connect_forwarders, get_event_bus
from app.shared.i18n import Locale
from app.shared.workflow.enums import Channel

from .deps import get_cms_service
from .enums import ConfigKind
from .errors import ConfigNotFoundError
from .schemas import (
    ChecklistDTO,
    ConfigRecordDTO,
    ConfigVersionDTO,
    RollbackRequest,
    UpsertChecklistRequest,
    UpsertConfigRequest,
    UpsertTemplateRequest,
    VariablesRequest,
)
from .service import CmsService


@asynccontextmanager
async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
    connect_forwarders(get_event_bus(), cms=get_cms_service().events)
    yield


# Admin service (cms.ai.madadfintech.com): gated by the admin bearer token + CORS.
app = create_service_app(
    title="MADAD CMS & Configuration Service",
    service="cms",
    admin=True,
    lifespan=lifespan,
)

Service = Annotated[CmsService, Depends(get_cms_service)]


# -- templates ---------------------------------------------------------------


@app.post("/cms/templates", response_model=ConfigRecordDTO)
async def upsert_template(req: UpsertTemplateRequest, service: Service) -> ConfigRecordDTO:
    record = await service.upsert_template(
        req.name,
        req.locale,
        req.body,
        channel=req.channel,
        variables=req.variables,
        comment=req.comment,
        updated_by=req.updated_by,
    )
    return ConfigRecordDTO.from_record(record)


@app.get("/cms/templates/{name}", response_model=ConfigRecordDTO)
async def get_template(
    name: str,
    service: Service,
    locale: Annotated[Locale, Query()] = Locale.EN,
    channel: Annotated[Channel | None, Query()] = None,
) -> ConfigRecordDTO:
    record = await service.get_template(name, locale, channel=channel)
    if record is None:
        raise ConfigNotFoundError(f"No template {name!r}", details={"name": name})
    return ConfigRecordDTO.from_record(record)


# -- generic configs ---------------------------------------------------------


@app.post("/cms/configs", response_model=ConfigRecordDTO)
async def upsert_config(req: UpsertConfigRequest, service: Service) -> ConfigRecordDTO:
    record = await service.upsert(
        req.kind,
        req.name,
        req.value,
        channel=req.channel,
        locale=req.locale,
        comment=req.comment,
        updated_by=req.updated_by,
    )
    return ConfigRecordDTO.from_record(record)


@app.get("/cms/configs/{kind}/{name}", response_model=ConfigRecordDTO)
async def get_config(
    kind: ConfigKind,
    name: str,
    service: Service,
    locale: Annotated[Locale | None, Query()] = None,
    channel: Annotated[Channel | None, Query()] = None,
) -> ConfigRecordDTO:
    record = await service.get(kind, name, channel=channel, locale=locale)
    if record is None:
        raise ConfigNotFoundError(f"No {kind} config {name!r}", details={"name": name})
    return ConfigRecordDTO.from_record(record)


@app.get("/cms/configs/{kind}/{name}/versions", response_model=list[ConfigVersionDTO])
async def list_versions(
    kind: ConfigKind,
    name: str,
    service: Service,
    locale: Annotated[Locale | None, Query()] = None,
    channel: Annotated[Channel | None, Query()] = None,
) -> list[ConfigVersionDTO]:
    versions = await service.list_versions(kind, name, channel=channel, locale=locale)
    return [ConfigVersionDTO.from_version(v) for v in versions]


@app.post("/cms/configs/{kind}/{name}/rollback", response_model=ConfigRecordDTO)
async def rollback(
    kind: ConfigKind, name: str, req: RollbackRequest, service: Service
) -> ConfigRecordDTO:
    record = await service.rollback(
        kind,
        name,
        req.target_version,
        channel=req.channel,
        locale=req.locale,
        updated_by=req.updated_by,
    )
    return ConfigRecordDTO.from_record(record)


# -- checklists --------------------------------------------------------------


@app.post("/cms/checklists/{name}", response_model=ConfigRecordDTO)
async def upsert_checklist(
    name: str, req: UpsertChecklistRequest, service: Service
) -> ConfigRecordDTO:
    record = await service.upsert_checklist(
        name, req.items, comment=req.comment, updated_by=req.updated_by
    )
    return ConfigRecordDTO.from_record(record)


@app.get("/cms/checklists/{name}", response_model=ChecklistDTO)
async def get_checklist(name: str, service: Service) -> ChecklistDTO:
    items = await service.get_checklist(name)
    return ChecklistDTO(name=name, items=items)


# -- runtime variables -------------------------------------------------------


@app.post("/cms/variables", response_model=ConfigRecordDTO)
async def set_variables(req: VariablesRequest, service: Service) -> ConfigRecordDTO:
    record = await service.set_variables(req.variables, updated_by=req.updated_by)
    return ConfigRecordDTO.from_record(record)


@app.get("/cms/variables")
async def get_variables(service: Service) -> dict[str, object]:
    return await service.get_variables()


# -- refresh -----------------------------------------------------------------


@app.post("/cms/refresh")
async def refresh(service: Service) -> dict[str, str]:
    await service.refresh()
    return {"status": "refreshed"}
