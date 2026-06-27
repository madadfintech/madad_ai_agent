"""Template provider port and rendering engine.

The CMS service *owns* template storage and management. The communication service
*renders* templates at send time. ``TemplateProvider`` is the seam to the CMS;
until the CMS lands, an in-memory provider is used. Rendering does safe
``{{ variable }}`` substitution and resolves the requested locale with fallback,
satisfying the Arabic/English requirement.
"""

from __future__ import annotations

import re
from abc import ABC, abstractmethod

from pydantic import BaseModel

from .enums import DEFAULT_LOCALE, Locale
from .errors import MissingTemplateVariableError, TemplateNotFoundError

# Matches {{ var }} / {{var}} with simple identifier names.
_VAR_PATTERN = re.compile(r"\{\{\s*([a-zA-Z_][a-zA-Z0-9_]*)\s*\}\}")


class Template(BaseModel):
    """A single template variant for one (key, locale)."""

    key: str
    locale: Locale
    body: str
    # Email subject line (UAT 2026-06-28, Ishan #A). Optional because most
    # templates don't carry one — WhatsApp ignores it, and the email path
    # falls back to a deterministic subject when this is None. Stored in
    # the CMS template's ``data.subject`` JSON field.
    subject: str | None = None


class TemplateProvider(ABC):
    """Port the CMS implements; returns a template variant or None."""

    @abstractmethod
    async def get(self, key: str, locale: Locale) -> Template | None: ...


class InMemoryTemplateProvider(TemplateProvider):
    """In-memory template store for tests/dev until the CMS service lands."""

    def __init__(self) -> None:
        self._templates: dict[tuple[str, Locale], Template] = {}

    def add(self, key: str, locale: Locale, body: str) -> None:
        self._templates[(key, locale)] = Template(key=key, locale=locale, body=body)

    async def get(self, key: str, locale: Locale) -> Template | None:
        return self._templates.get((key, locale))


def render(body: str, variables: dict[str, object], *, strict: bool = True) -> str:
    """Substitute ``{{ var }}`` placeholders from ``variables``.

    In strict mode (default) an unknown placeholder raises
    :class:`MissingTemplateVariableError`; otherwise it is left as-is.
    """

    missing: list[str] = []

    def _replace(match: re.Match[str]) -> str:
        name = match.group(1)
        if name in variables:
            return str(variables[name])
        if strict:
            missing.append(name)
            return match.group(0)
        return match.group(0)

    result = _VAR_PATTERN.sub(_replace, body)
    if strict and missing:
        raise MissingTemplateVariableError(
            f"Missing template variable(s): {', '.join(sorted(set(missing)))}",
            details={"missing": sorted(set(missing))},
        )
    return result


class TemplateRenderer:
    """Resolves a template by key+locale (with fallback) and renders it."""

    def __init__(
        self,
        provider: TemplateProvider,
        *,
        default_locale: Locale = DEFAULT_LOCALE,
        strict: bool = True,
    ) -> None:
        self._provider = provider
        self._default_locale = default_locale
        self._strict = strict

    async def render_template(
        self,
        key: str,
        variables: dict[str, object],
        *,
        locale: Locale | None = None,
    ) -> str:
        """Render template ``key`` in ``locale`` (falling back to the default).

        Returns the rendered BODY only — use :meth:`render_with_subject` when
        the caller also needs the email subject line.
        """
        rendered, _ = await self.render_with_subject(key, variables, locale=locale)
        return rendered

    async def render_with_subject(
        self,
        key: str,
        variables: dict[str, object],
        *,
        locale: Locale | None = None,
    ) -> tuple[str, str | None]:
        """Render the template and return ``(body, subject)`` — both with
        ``{{ variables }}`` substituted. Subject is None when the template
        doesn't define one (most do not — only email-specific templates).
        """
        requested = locale or self._default_locale
        template = await self._provider.get(key, requested)
        if template is None and requested != self._default_locale:
            template = await self._provider.get(key, self._default_locale)
        if template is None:
            raise TemplateNotFoundError(
                f"No template {key!r} for locale {requested}",
                details={"key": key, "locale": str(requested)},
            )
        body = render(template.body, variables, strict=self._strict)
        # Subject is rendered non-strict — a template missing a variable
        # in its body must hard-fail (the SME gets wrong content) but a
        # subject missing a variable can fall back to the default
        # subject at the gateway. Avoids a half-rendered subject like
        # "Madad — your {{ amount }} is due".
        subject = (
            render(template.subject, variables, strict=False)
            if template.subject
            else None
        )
        return body, subject
