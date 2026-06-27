"""CMS & dynamic configuration orchestration.

Read path: cache -> store (on miss), so live conversations resolve content fast.
Write path: validate -> append new version -> invalidate cache -> emit event ->
audit. The invalidate + short cache TTL are what satisfy the <5-minute
propagation requirement (the Redis cache adapter broadcasts evictions across
instances in production).

Typed accessors layer over the generic versioned store for templates, checklists,
and runtime variables.
"""

from __future__ import annotations

from typing import Any

from app.core.logging import get_logger
from app.shared.i18n import DEFAULT_LOCALE, Locale
from app.shared.workflow.enums import Channel

from .audit import CmsAuditLogger
from .cache import ConfigCache
from .enums import ConfigKind
from .errors import ConfigNotFoundError, ConfigVersionNotFoundError
from .events import CmsEvent, CmsEventBus, CmsEventType
from .models import (
    ChecklistItem,
    ConfigKey,
    ConfigRecord,
    ConfigVersion,
    checklist_value,
    parse_checklist,
)
from .persistence import ConfigStore
from .validation import validate

# Conventional name for the global runtime-variables setting.
GLOBAL_VARIABLES = "global_variables"


class CmsService:
    """Runtime-configurable content & config platform (no-code operational updates)."""

    def __init__(
        self,
        *,
        store: ConfigStore,
        cache: ConfigCache,
        events: CmsEventBus,
        audit: CmsAuditLogger,
        default_locale: Locale = DEFAULT_LOCALE,
        logger: Any | None = None,
    ) -> None:
        self._store = store
        self._cache = cache
        self._events = events
        self._audit = audit
        self._default_locale = default_locale
        self._log = logger or get_logger("cms.service")

    @property
    def events(self) -> CmsEventBus:
        """The in-process event bus (for forwarding onto the unified bus)."""

        return self._events

    # -- generic config -------------------------------------------------------

    async def upsert(
        self,
        kind: ConfigKind,
        name: str,
        value: dict[str, Any],
        *,
        channel: Channel | None = None,
        locale: Locale | None = None,
        comment: str | None = None,
        updated_by: str | None = None,
    ) -> ConfigRecord:
        """Validate and store a new version of a config entry."""

        validate(kind, value)
        key = ConfigKey(kind, name, channel, locale)
        record = await self._store.save_new_version(
            key, value, comment=comment, updated_by=updated_by
        )
        await self._cache.invalidate(key.identity())
        await self._emit(CmsEventType.CONFIG_UPDATED, record)
        await self._audit.record(
            "upsert", kind=kind, name=name, version=record.version, updated_by=updated_by
        )
        return record

    async def get(
        self,
        kind: ConfigKind,
        name: str,
        *,
        channel: Channel | None = None,
        locale: Locale | None = None,
        use_cache: bool = True,
    ) -> ConfigRecord | None:
        """Return the current record for a key (cached), or None."""

        key = ConfigKey(kind, name, channel, locale)
        cache_id = key.identity()
        if use_cache:
            cached = await self._cache.get(cache_id)
            if cached is not None:
                return cached
        record = await self._store.get_current(key)
        if record is not None and use_cache:
            await self._cache.set(cache_id, record)
        return record

    async def require(
        self,
        kind: ConfigKind,
        name: str,
        *,
        channel: Channel | None = None,
        locale: Locale | None = None,
    ) -> ConfigRecord:
        record = await self.get(kind, name, channel=channel, locale=locale)
        if record is None:
            raise ConfigNotFoundError(
                f"No {kind} config {name!r}",
                details={"kind": str(kind), "name": name},
            )
        return record

    async def rollback(
        self,
        kind: ConfigKind,
        name: str,
        target_version: int,
        *,
        channel: Channel | None = None,
        locale: Locale | None = None,
        updated_by: str | None = None,
    ) -> ConfigRecord:
        """Restore a prior version by appending it as a new current version."""

        key = ConfigKey(kind, name, channel, locale)
        target = await self._store.get_version(key, target_version)
        if target is None:
            raise ConfigVersionNotFoundError(
                f"No version {target_version} for {kind} {name!r}",
                details={"kind": str(kind), "name": name, "version": target_version},
            )
        record = await self._store.save_new_version(
            key,
            target.value,
            comment=f"rollback to v{target_version}",
            updated_by=updated_by,
        )
        await self._cache.invalidate(key.identity())
        await self._emit(CmsEventType.CONFIG_ROLLED_BACK, record)
        await self._audit.record(
            "rollback",
            kind=kind,
            name=name,
            version=record.version,
            updated_by=updated_by,
            detail={"restored_from": target_version},
        )
        return record

    async def delete(
        self,
        kind: ConfigKind,
        name: str,
        *,
        channel: Channel | None = None,
        locale: Locale | None = None,
        updated_by: str | None = None,
    ) -> bool:
        key = ConfigKey(kind, name, channel, locale)
        existed = await self._store.delete_current(key)
        await self._cache.invalidate(key.identity())
        if existed:
            await self._events.publish(
                CmsEvent(
                    type=CmsEventType.CONFIG_DELETED,
                    kind=kind,
                    name=name,
                    channel=channel,
                    locale=locale,
                )
            )
            await self._audit.record("delete", kind=kind, name=name, updated_by=updated_by)
        return existed

    async def list_versions(
        self,
        kind: ConfigKind,
        name: str,
        *,
        channel: Channel | None = None,
        locale: Locale | None = None,
    ) -> list[ConfigVersion]:
        return await self._store.list_versions(ConfigKey(kind, name, channel, locale))

    async def list_keys(self, kind: ConfigKind | None = None) -> list[ConfigKey]:
        return await self._store.list_keys(kind)

    async def refresh(self) -> None:
        """Drop the entire cache so the next reads reload from the store."""

        await self._cache.clear()
        await self._events.publish(CmsEvent(type=CmsEventType.CONFIG_REFRESHED))
        await self._audit.record("refresh")

    # -- templates (channel + locale aware, with fallback) --------------------

    async def upsert_template(
        self,
        name: str,
        locale: Locale,
        body: str,
        *,
        channel: Channel | None = None,
        variables: list[str] | None = None,
        comment: str | None = None,
        updated_by: str | None = None,
        subject: str | None = None,
        buttons: list[dict[str, Any]] | None = None,
    ) -> ConfigRecord:
        value: dict[str, Any] = {"body": body}
        if variables is not None:
            value["variables"] = variables
        # Email subject (UAT 2026-06-28, Ishan #A) — optional; rendered
        # by ``TemplateRenderer.render_with_subject`` with the same
        # ``{{ variable }}`` bag the body uses. Gateway picks it up from
        # ``metadata.subject``; WhatsApp gateway ignores it.
        if subject is not None:
            value["subject"] = subject
        # Reply-button labels (UAT 2026-06-28, Ishan #2) — optional;
        # consumed by ``OnboardingWorkflow._resolve_buttons``. See
        # ``OnboardingWorkflow.BUTTON_DEFAULTS`` for the stable ids
        # the agent listens for (the portal editor pins those).
        if buttons is not None:
            value["buttons"] = buttons
        return await self.upsert(
            ConfigKind.TEMPLATE,
            name,
            value,
            channel=channel,
            locale=locale,
            comment=comment,
            updated_by=updated_by,
        )

    async def get_template(
        self,
        name: str,
        locale: Locale,
        *,
        channel: Channel | None = None,
    ) -> ConfigRecord | None:
        """Resolve a template with channel + locale fallback.

        Order: (channel, locale) -> (any-channel, locale) -> (channel, default)
        -> (any-channel, default).
        """

        attempts: list[tuple[Channel | None, Locale]] = [(channel, locale)]
        if channel is not None:
            attempts.append((None, locale))
        if locale != self._default_locale:
            attempts.append((channel, self._default_locale))
            if channel is not None:
                attempts.append((None, self._default_locale))

        for ch, loc in attempts:
            record = await self.get(ConfigKind.TEMPLATE, name, channel=ch, locale=loc)
            if record is not None:
                return record
        return None

    async def get_template_body(
        self, name: str, locale: Locale, *, channel: Channel | None = None
    ) -> str | None:
        record = await self.get_template(name, locale, channel=channel)
        return None if record is None else str(record.value.get("body"))

    # -- checklists -----------------------------------------------------------

    async def upsert_checklist(
        self,
        name: str,
        items: list[ChecklistItem],
        *,
        comment: str | None = None,
        updated_by: str | None = None,
    ) -> ConfigRecord:
        return await self.upsert(
            ConfigKind.CHECKLIST,
            name,
            checklist_value(items),
            comment=comment,
            updated_by=updated_by,
        )

    async def get_checklist(self, name: str) -> list[ChecklistItem]:
        record = await self.get(ConfigKind.CHECKLIST, name)
        return [] if record is None else parse_checklist(record.value)

    # -- runtime variables ----------------------------------------------------

    async def set_variables(
        self,
        variables: dict[str, Any],
        *,
        name: str = GLOBAL_VARIABLES,
        updated_by: str | None = None,
    ) -> ConfigRecord:
        return await self.upsert(
            ConfigKind.SETTING, name, dict(variables), updated_by=updated_by
        )

    async def get_variables(self, *, name: str = GLOBAL_VARIABLES) -> dict[str, Any]:
        record = await self.get(ConfigKind.SETTING, name)
        return dict(record.value) if record is not None else {}

    # -- helpers --------------------------------------------------------------

    async def _emit(self, event_type: CmsEventType, record: ConfigRecord) -> None:
        await self._events.publish(
            CmsEvent(
                type=event_type,
                kind=record.kind,
                name=record.name,
                channel=record.channel,
                locale=record.locale,
                version=record.version,
            )
        )
