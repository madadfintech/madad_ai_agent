"""Bridge: make CMS templates consumable by the Communication service.

The Communication service depends on a ``TemplateProvider`` port. This adapter
implements that port on top of the CMS, so wiring becomes::

    comms = build_communication_service(template_provider=CmsTemplateProvider(cms))

This is the integration seam noted when the Communication service was built. The
adapter resolves channel + locale via the CMS's fallback logic.
"""

from __future__ import annotations

from app.services.communication.templating import Template, TemplateProvider
from app.shared.i18n import Locale
from app.shared.workflow.enums import Channel

from .service import CmsService


class CmsTemplateProvider(TemplateProvider):
    """Implements the Communication ``TemplateProvider`` port using the CMS.

    Optionally bound to a ``channel`` so channel-specific templates resolve; if
    unbound, channel-agnostic templates are used.
    """

    def __init__(self, cms: CmsService, *, channel: Channel | None = None) -> None:
        self._cms = cms
        self._channel = channel

    async def get(self, key: str, locale: Locale) -> Template | None:
        record = await self._cms.get_template(key, locale, channel=self._channel)
        if record is None:
            return None
        value = record.value if isinstance(record.value, dict) else {}
        # Optional ``subject`` flows through for email-channel sends;
        # absent / non-string → None and the gateway uses its default.
        raw_subject = value.get("subject")
        subject = (
            raw_subject if isinstance(raw_subject, str) and raw_subject else None
        )
        return Template(
            key=key,
            locale=record.locale or locale,
            body=str(value["body"]),
            subject=subject,
        )
