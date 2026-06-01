"""Dynamic document checklist — CMS-driven required-document tracking.

The set of required documents is operated by MADAD via the CMS (``CHECKLIST``
kind), so adding a new required document reflects in onboarding with no code
change. This module resolves the required list (via a port) and computes what's
received / validated / still missing.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import TYPE_CHECKING

from pydantic import BaseModel, Field

if TYPE_CHECKING:  # pragma: no cover
    from app.services.cms.service import CmsService


class RequiredDocument(BaseModel):
    code: str
    required: bool = True


class ChecklistStatus(BaseModel):
    """Computed completion state of a checklist for a target."""

    checklist: str
    received: list[str] = Field(default_factory=list)
    validated: list[str] = Field(default_factory=list)
    missing: list[str] = Field(default_factory=list)
    rejected: list[str] = Field(default_factory=list)

    @property
    def complete(self) -> bool:
        return not self.missing


class ChecklistProvider(ABC):
    """Port returning the required documents for a checklist."""

    @abstractmethod
    async def get_required(self, checklist: str) -> list[RequiredDocument]: ...


class InMemoryChecklistProvider(ChecklistProvider):
    def __init__(self) -> None:
        self._checklists: dict[str, list[RequiredDocument]] = {}

    def add(self, checklist: str, codes: list[str]) -> None:
        self._checklists[checklist] = [RequiredDocument(code=c) for c in codes]

    async def get_required(self, checklist: str) -> list[RequiredDocument]:
        return list(self._checklists.get(checklist, []))


class CmsChecklistProvider(ChecklistProvider):
    """Reads the required-document list from the CMS (``CHECKLIST`` kind)."""

    def __init__(self, cms: CmsService) -> None:
        self._cms = cms

    async def get_required(self, checklist: str) -> list[RequiredDocument]:
        items = await self._cms.get_checklist(checklist)
        return [RequiredDocument(code=i.code, required=i.required) for i in items]
