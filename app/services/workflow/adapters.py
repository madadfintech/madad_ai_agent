"""Real-service adapters for the onboarding ports.

Bridge the workflow's ports to the already-built services. Madad/Tess have no
real adapter yet (MCP-blocked); the Document adapter needs media fetch (also MCP)
so the in-memory intake is used until then.
"""

from __future__ import annotations

from typing import Any

from app.core.logging import get_logger
from app.services.communication.schemas import OutboundMessageRequest
from app.services.communication.service import CommunicationService
from app.services.nudge.service import NudgeService
from app.shared.workflow.enums import Channel

from .ports import Messenger, Reminders

# [TEMP-DBG] To find exact bug - Temp Logs (behavioral instrumentation):
# Every outbound send logs a structured ``obs.outbound.send`` line so the
# log monitor's behavioral rules can detect:
#   * Duplicate template fires (same identity + template_key N+ in window)
#   * Empty-variable canary (≥2 of the template's variables render as em-dash)
# Remove these when the pipeline is stable.
_OBS = get_logger("workflow.obs")


def _count_em_dashes(variables: dict[str, Any] | None) -> int:
    """Count how many variable values would render as a literal em-dash.
    Templates that fire with multiple ``—`` placeholders are usually a
    bug (template variable wiring missed a field)."""
    if not variables:
        return 0
    n = 0
    for v in variables.values():
        if isinstance(v, str) and v.strip() in ("—", ""):
            n += 1
        elif v is None:
            n += 1
    return n


def _emit_obs_send(
    *, identity: str, channel: Any, template_key: str,
    variables: dict[str, Any] | None, send_kind: str,
) -> None:
    """[TEMP-DBG] Emit one ``obs.outbound.send`` line per send for the
    behavioral monitor. Best-effort; never raise into the messenger path."""
    try:
        em_dash = _count_em_dashes(variables)
        total = len(variables or {})
        _OBS.info(
            "[TEMP-DBG] obs.outbound.send",
            identity=identity,
            channel=str(getattr(channel, "value", channel)).lower(),
            template_key=template_key,
            send_kind=send_kind,
            vars_em_dash_count=em_dash,
            vars_total=total,
        )
    except Exception:  # noqa: BLE001 — instrumentation must never fail
        pass


class CommunicationMessenger(Messenger):
    """Sends outbound messages through the Communication service (renders CMS
    templates, dispatches via MCP)."""

    def __init__(self, comms: CommunicationService) -> None:
        self._comms = comms

    async def send(
        self,
        *,
        channel: Channel,
        identity: str,
        template_key: str,
        variables: dict[str, Any] | None = None,
        locale: str = "en",
    ) -> None:
        from app.shared.i18n import Locale

        _emit_obs_send(
            identity=identity, channel=channel,
            template_key=template_key, variables=variables, send_kind="text",
        )
        await self._comms.send(
            OutboundMessageRequest(
                channel=channel,
                identity=identity,
                template_key=template_key,
                variables=variables or {},
                locale=Locale(locale),
            )
        )

    async def send_cta_url(
        self,
        *,
        channel: Channel,
        identity: str,
        template_key: str,
        button_text: str,
        button_url: str,
        variables: dict[str, Any] | None = None,
        locale: str = "en",
    ) -> bool:
        from app.services.communication.models import MessageStatus  # type: ignore[attr-defined]
        from app.shared.i18n import Locale

        _emit_obs_send(
            identity=identity, channel=channel,
            template_key=template_key, variables=variables, send_kind="cta_url",
        )
        message = await self._comms.send(
            OutboundMessageRequest(
                channel=channel,
                identity=identity,
                template_key=template_key,
                variables=variables or {},
                locale=Locale(locale),
                metadata={
                    "cta": {"button_text": button_text, "button_url": button_url}
                },
            )
        )
        # Only report success if the interactive send actually went out — the
        # caller falls back to a plain-text message otherwise (e.g. when the
        # backend interactive endpoint / MCP tool is not yet live).
        return getattr(message, "status", None) == MessageStatus.SENT

    async def send_reply_buttons(
        self,
        *,
        channel: Channel,
        identity: str,
        template_key: str,
        buttons: list[tuple[str, str]],
        variables: dict[str, Any] | None = None,
        locale: str = "en",
    ) -> bool:
        """Send up to 3 interactive reply (quick-reply) buttons. ``buttons`` is
        a list of ``(id, title)`` pairs. Returns True only if the interactive
        send went out; the caller falls back to plain text otherwise (backend
        ``interactive-buttons`` endpoint / MCP tool not yet live)."""
        from app.services.communication.models import MessageStatus  # type: ignore[attr-defined]
        from app.shared.i18n import Locale

        _emit_obs_send(
            identity=identity, channel=channel,
            template_key=template_key, variables=variables,
            send_kind="reply_buttons",
        )
        message = await self._comms.send(
            OutboundMessageRequest(
                channel=channel,
                identity=identity,
                template_key=template_key,
                variables=variables or {},
                locale=Locale(locale),
                metadata={
                    "interactive_buttons": {
                        "buttons": [
                            {"id": bid, "title": title} for bid, title in buttons
                        ]
                    }
                },
            )
        )
        return getattr(message, "status", None) == MessageStatus.SENT

    async def send_template(
        self,
        *,
        channel: Channel,
        identity: str,
        template_name: str,
        template_key: str,
        language_code: str = "en",
        variables: dict[str, Any] | None = None,
        components: list[dict[str, Any]] | None = None,
    ) -> bool:
        from app.services.communication.models import MessageStatus  # type: ignore[attr-defined]
        from app.shared.i18n import Locale

        _emit_obs_send(
            identity=identity, channel=channel,
            template_key=template_key, variables=variables,
            send_kind="meta_template",
        )
        message = await self._comms.send(
            OutboundMessageRequest(
                channel=channel,
                identity=identity,
                template_key=template_key,
                variables=variables or {},
                locale=Locale(language_code if len(language_code) == 2 else "en"),
                metadata={
                    "whatsapp_template": {
                        "name": template_name,
                        "language": language_code,
                        **({"components": components} if components else {}),
                    }
                },
            )
        )
        return getattr(message, "status", None) == MessageStatus.SENT

    async def send_document(
        self,
        *,
        channel: Channel,
        identity: str,
        filename: str,
        content_base64: str,
        caption: str,
        mime_type: str = "text/csv",
        locale: str = "en",
    ) -> bool:
        """Send a document (file attachment) through the Communication
        service. The gateway picks the ``madad_external_send_whatsapp_document``
        MCP tool from the ``document`` metadata block. ``caption`` doubles as
        the required message text (CMS templating is bypassed for documents).
        Returns True only if the document actually went out — the caller falls
        back to an inline table otherwise (backend document route not live)."""
        from app.services.communication.models import MessageStatus  # type: ignore[attr-defined]
        from app.shared.i18n import Locale

        _emit_obs_send(
            identity=identity, channel=channel,
            template_key="(document)", variables=None, send_kind="document",
        )
        message = await self._comms.send(
            OutboundMessageRequest(
                channel=channel,
                identity=identity,
                text=caption,
                locale=Locale(locale),
                metadata={
                    "document": {
                        "filename": filename,
                        "content_base64": content_base64,
                        "mime_type": mime_type,
                        "caption": caption,
                    }
                },
            )
        )
        return getattr(message, "status", None) == MessageStatus.SENT


class NudgeReminders(Reminders):
    """Schedules/suppresses reminder sequences through the Nudge service."""

    def __init__(self, nudge: NudgeService) -> None:
        self._nudge = nudge

    async def schedule(
        self,
        reason: str,
        *,
        channel: Channel,
        identity: str,
        target_ref: str | None,
        variables: dict[str, Any] | None = None,
    ) -> None:
        from app.services.nudge.errors import ScheduleNotFoundError

        try:
            await self._nudge.start_sequence(
                reason,
                {channel: identity},
                variables=variables,
                target_ref=target_ref,
            )
        except ScheduleNotFoundError:
            # A reason with no configured schedule must not break onboarding.
            return

    async def suppress(self, *, target_ref: str | None) -> None:
        if target_ref is not None:
            await self._nudge.suppress_matching(target_ref=target_ref)
