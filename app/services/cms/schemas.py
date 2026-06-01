"""Admin API DTOs for the CMS service."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field

from app.shared.i18n import Locale
from app.shared.workflow.enums import Channel

from .enums import ConfigKind
from .models import ChecklistItem, ConfigRecord, ConfigVersion


class UpsertTemplateRequest(BaseModel):
    name: str
    locale: Locale
    body: str
    channel: Channel | None = None
    variables: list[str] | None = None
    comment: str | None = None
    updated_by: str | None = None


class UpsertConfigRequest(BaseModel):
    kind: ConfigKind
    name: str
    value: dict[str, Any]
    channel: Channel | None = None
    locale: Locale | None = None
    comment: str | None = None
    updated_by: str | None = None


class UpsertChecklistRequest(BaseModel):
    items: list[ChecklistItem]
    comment: str | None = None
    updated_by: str | None = None


class RollbackRequest(BaseModel):
    target_version: int
    channel: Channel | None = None
    locale: Locale | None = None
    updated_by: str | None = None


class VariablesRequest(BaseModel):
    variables: dict[str, Any]
    updated_by: str | None = None


class ConfigRecordDTO(BaseModel):
    kind: ConfigKind
    name: str
    channel: Channel | None
    locale: Locale | None
    version: int
    value: dict[str, Any]
    comment: str | None
    updated_by: str | None
    updated_at: datetime

    @classmethod
    def from_record(cls, record: ConfigRecord) -> ConfigRecordDTO:
        return cls(
            kind=record.kind,
            name=record.name,
            channel=record.channel,
            locale=record.locale,
            version=record.version,
            value=record.value,
            comment=record.comment,
            updated_by=record.updated_by,
            updated_at=record.updated_at,
        )


class ConfigVersionDTO(BaseModel):
    version: int
    value: dict[str, Any]
    comment: str | None
    updated_by: str | None
    created_at: datetime

    @classmethod
    def from_version(cls, version: ConfigVersion) -> ConfigVersionDTO:
        return cls(
            version=version.version,
            value=version.value,
            comment=version.comment,
            updated_by=version.updated_by,
            created_at=version.created_at,
        )


class ChecklistDTO(BaseModel):
    name: str
    items: list[ChecklistItem] = Field(default_factory=list)
