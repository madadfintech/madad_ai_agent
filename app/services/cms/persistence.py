"""Versioned configuration store.

Append-only: every save creates a new version and updates the current pointer.
History is retained for rollback and audit. In-memory adapter now; Postgres
(``cms`` schema) lands with the platform DB foundation.
"""

from __future__ import annotations

import asyncio
from abc import ABC, abstractmethod
from typing import Any

from app.shared.workflow.utils import utcnow

from .enums import ConfigKind
from .models import ConfigKey, ConfigRecord, ConfigVersion


class ConfigStore(ABC):
    @abstractmethod
    async def get_current(self, key: ConfigKey) -> ConfigRecord | None: ...

    @abstractmethod
    async def save_new_version(
        self,
        key: ConfigKey,
        value: dict[str, Any],
        *,
        comment: str | None = None,
        updated_by: str | None = None,
    ) -> ConfigRecord: ...

    @abstractmethod
    async def list_versions(self, key: ConfigKey) -> list[ConfigVersion]: ...

    @abstractmethod
    async def get_version(self, key: ConfigKey, version: int) -> ConfigVersion | None: ...

    @abstractmethod
    async def delete_current(self, key: ConfigKey) -> bool: ...

    @abstractmethod
    async def list_keys(self, kind: ConfigKind | None = None) -> list[ConfigKey]: ...


class InMemoryConfigStore(ConfigStore):
    def __init__(self) -> None:
        self._current: dict[str, ConfigRecord] = {}
        self._history: dict[str, list[ConfigVersion]] = {}
        self._keys: dict[str, ConfigKey] = {}
        self._lock = asyncio.Lock()

    async def get_current(self, key: ConfigKey) -> ConfigRecord | None:
        async with self._lock:
            record = self._current.get(key.identity())
            return record.model_copy(deep=True) if record else None

    async def save_new_version(
        self,
        key: ConfigKey,
        value: dict[str, Any],
        *,
        comment: str | None = None,
        updated_by: str | None = None,
    ) -> ConfigRecord:
        ident = key.identity()
        async with self._lock:
            history = self._history.setdefault(ident, [])
            existing = self._current.get(ident)
            version = (history[-1].version + 1) if history else 1
            now = utcnow()
            record = ConfigRecord(
                kind=key.kind,
                name=key.name,
                channel=key.channel,
                locale=key.locale,
                version=version,
                value=value,
                comment=comment,
                updated_by=updated_by,
                created_at=existing.created_at if existing else now,
                updated_at=now,
            )
            self._current[ident] = record.model_copy(deep=True)
            self._keys[ident] = key
            history.append(
                ConfigVersion(
                    kind=key.kind,
                    name=key.name,
                    channel=key.channel,
                    locale=key.locale,
                    version=version,
                    value=value,
                    comment=comment,
                    updated_by=updated_by,
                    created_at=now,
                )
            )
            return record.model_copy(deep=True)

    async def list_versions(self, key: ConfigKey) -> list[ConfigVersion]:
        async with self._lock:
            return [v.model_copy(deep=True) for v in self._history.get(key.identity(), [])]

    async def get_version(self, key: ConfigKey, version: int) -> ConfigVersion | None:
        async with self._lock:
            for v in self._history.get(key.identity(), []):
                if v.version == version:
                    return v.model_copy(deep=True)
            return None

    async def delete_current(self, key: ConfigKey) -> bool:
        async with self._lock:
            existed = self._current.pop(key.identity(), None) is not None
            # History is intentionally retained for audit.
            return existed

    async def list_keys(self, kind: ConfigKind | None = None) -> list[ConfigKey]:
        async with self._lock:
            keys = [self._keys[i] for i in self._current]
        return [k for k in keys if kind is None or k.kind == kind]
