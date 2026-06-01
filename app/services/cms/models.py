"""CMS domain models.

A single versioned store backs every config kind. An entry is addressed by a
:class:`ConfigKey` (kind + name, plus channel/locale for templated content). Each
update appends an immutable :class:`ConfigVersion`; the latest is exposed as the
current :class:`ConfigRecord`. This append-only history is what powers rollback
and audit.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field

from app.shared.i18n import Locale
from app.shared.workflow.enums import Channel
from app.shared.workflow.utils import new_id, utcnow

from .enums import ConfigKind


@dataclass(frozen=True)
class ConfigKey:
    """Stable address of a configuration entry.

    ``channel`` and ``locale`` are used for templated content; other kinds leave
    them unset.
    """

    kind: ConfigKind
    name: str
    channel: Channel | None = None
    locale: Locale | None = None

    def identity(self) -> str:
        return f"{self.kind}:{self.name}:{self.channel or '*'}:{self.locale or '*'}"


class ConfigVersion(BaseModel):
    """An immutable historical snapshot of a config entry."""

    version_id: str = Field(default_factory=lambda: new_id("cfgv"))
    kind: ConfigKind
    name: str
    channel: Channel | None = None
    locale: Locale | None = None
    version: int
    value: dict[str, Any]
    comment: str | None = None
    updated_by: str | None = None
    created_at: datetime = Field(default_factory=utcnow)


class ConfigRecord(BaseModel):
    """The current (latest) state of a config entry."""

    kind: ConfigKind
    name: str
    channel: Channel | None = None
    locale: Locale | None = None
    version: int
    value: dict[str, Any]
    comment: str | None = None
    updated_by: str | None = None
    created_at: datetime = Field(default_factory=utcnow)
    updated_at: datetime = Field(default_factory=utcnow)

    def key(self) -> ConfigKey:
        return ConfigKey(self.kind, self.name, self.channel, self.locale)

    def cache_id(self) -> str:
        return self.key().identity()


class ChecklistItem(BaseModel):
    """One required document in a dynamic checklist.

    ``label`` is a locale->text map so a single checklist serves both languages.
    """

    code: str
    label: dict[str, str] = Field(default_factory=dict)
    required: bool = True
    category: str | None = None


def checklist_value(items: list[ChecklistItem]) -> dict[str, Any]:
    """Build a checklist config value from typed items."""

    return {"items": [item.model_dump() for item in items]}


def parse_checklist(value: dict[str, Any]) -> list[ChecklistItem]:
    """Parse a checklist config value back into typed items."""

    return [ChecklistItem.model_validate(item) for item in value.get("items", [])]
