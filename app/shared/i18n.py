"""Platform-wide internationalization primitives.

``Locale`` is a cross-cutting value type (the Communication, CMS, and future
services all use it), so it lives here in shared rather than inside any one
service. Arabic and English are the Phase 1 requirement.
"""

from __future__ import annotations

from enum import StrEnum


class Locale(StrEnum):
    EN = "en"
    AR = "ar"


DEFAULT_LOCALE = Locale.EN


def resolve_locale(value: str | None, *, default: Locale = DEFAULT_LOCALE) -> Locale:
    """Best-effort parse of a locale string, falling back to the default."""

    if not value:
        return default
    candidate = value.strip().lower()[:2]
    try:
        return Locale(candidate)
    except ValueError:
        return default


__all__ = ["Locale", "DEFAULT_LOCALE", "resolve_locale"]
