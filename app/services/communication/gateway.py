"""MCP communication gateway — the outbound dispatch interface.

The actual WhatsApp/Email APIs live behind the external MCP cluster (owned by a
separate engineer). This module defines the *interface* the service dispatches
through and adapters for it. We never call Meta/SendGrid directly.

* ``CommunicationGateway`` — the port the service depends on.
* ``InMemoryCommunicationGateway`` — records sends; used in tests/dev.
* ``McpCommunicationGateway`` — maps a send to an MCP tool call via an injected
  ``MCPToolCaller``. Tool names are PROVISIONAL pending the MCP team's catalog.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any

import structlog
from pydantic import BaseModel

from app.shared.mcp import MCPToolCaller, Tools
from app.shared.workflow.enums import Channel
from app.shared.workflow.utils import new_id

from .errors import GatewayError
from .models import Message

_LOG = structlog.get_logger(__name__)


class OutboundDispatchResult(BaseModel):
    """Result of handing a message to the channel layer."""

    accepted: bool
    provider_message_id: str | None = None
    raw: dict[str, Any] = {}


class CommunicationGateway(ABC):
    """Port for dispatching an outbound message to the channel layer."""

    @abstractmethod
    async def send(self, message: Message) -> OutboundDispatchResult: ...


class InMemoryCommunicationGateway(CommunicationGateway):
    """Records outbound messages; can simulate transient failures for tests.

    ``fail_times`` makes the next N sends raise :class:`GatewayError` before
    succeeding — used to exercise retry behaviour.
    """

    def __init__(self, *, fail_times: int = 0) -> None:
        self.sent: list[Message] = []
        self._fail_times = fail_times
        self._calls = 0

    async def send(self, message: Message) -> OutboundDispatchResult:
        self._calls += 1
        if self._calls <= self._fail_times:
            raise GatewayError(f"simulated transient gateway failure #{self._calls}")
        self.sent.append(message)
        return OutboundDispatchResult(
            accepted=True,
            provider_message_id=new_id("prov"),
            raw={"echo": message.text},
        )


# Tool names come from the shared registry (single source, catalog-reconciled).
# WhatsApp maps to the backend-routed free-text WhatsApp tool. Email currently
# maps to the OTP-only tool — the cluster has no arbitrary-email-send tool yet
# (Q2: arbitrary email content is not exposed via MCP). Refine in Phase 2 when
# the workflow distinguishes OTP-style sends from informational sends.
_TOOL_BY_CHANNEL: dict[Channel, str] = {
    Channel.WHATSAPP: Tools.EXT_SEND_WHATSAPP_TEXT,
    # Arbitrary-content email send (subject + body), NOT the OTP tool — so the
    # agent can drive a full email onboarding thread, not just verification codes.
    Channel.EMAIL: Tools.EXT_SEND_EMAIL_TEXT,
}

# Default subject for agent-driven email when the message carries none in its
# metadata. Per-step subjects can be supplied via message.metadata["subject"].
_DEFAULT_EMAIL_SUBJECT = "Madad Financing — update on your application"


class McpCommunicationGateway(CommunicationGateway):
    """Dispatches outbound messages by calling MCP tools."""

    def __init__(self, tool_caller: MCPToolCaller) -> None:
        self._tools = tool_caller

    async def send(self, message: Message) -> OutboundDispatchResult:
        # Approved WhatsApp TEMPLATE (e.g. the "initiate" reach-out): the FIRST
        # outbound to a contact (and any message outside Meta's 24h
        # customer-service window) MUST be a pre-approved template, not free
        # text. Selected when the outbound carries a ``whatsapp_template`` block
        # in its metadata. Free-text would be silently rejected by Meta.
        wat = (message.metadata or {}).get("whatsapp_template") if message.metadata else None
        if message.channel is Channel.WHATSAPP and isinstance(wat, dict) and wat.get("name"):
            tool = Tools.EXT_SEND_WHATSAPP_TEMPLATE
            tpl_payload: dict[str, Any] = {
                "to": message.identity,
                "template_name": wat["name"],
                "language_code": wat.get("language") or "en",
            }
            if wat.get("components"):
                tpl_payload["components"] = wat["components"]
            try:
                response = await self._tools.call_tool(tool, tpl_payload)
            except Exception as exc:  # noqa: BLE001 - normalize transport errors
                raise GatewayError(
                    f"MCP tool {tool!r} failed: {exc}", details={"tool": tool}
                ) from exc
            return OutboundDispatchResult(
                accepted=bool(response.get("accepted", True)),
                provider_message_id=response.get("provider_message_id"),
                raw=response,
            )

        # Document attachment (WhatsApp): e.g. the bulk-invoice review CSV the
        # SME edits and sends back. Selected when the outbound carries a
        # ``document`` block with base64 bytes in its metadata.
        doc = (message.metadata or {}).get("document") if message.metadata else None
        if (
            message.channel is Channel.WHATSAPP
            and isinstance(doc, dict)
            and doc.get("content_base64")
        ):
            tool = Tools.EXT_SEND_WHATSAPP_DOCUMENT
            doc_payload: dict[str, Any] = {
                "to": message.identity,
                "filename": doc.get("filename") or "document.csv",
                "content_base64": doc["content_base64"],
            }
            if doc.get("mime_type"):
                doc_payload["mime_type"] = doc["mime_type"]
            if doc.get("caption"):
                doc_payload["caption"] = doc["caption"]
            try:
                response = await self._tools.call_tool(tool, doc_payload)
            except Exception as exc:  # noqa: BLE001 - normalize transport errors
                raise GatewayError(
                    f"MCP tool {tool!r} failed: {exc}", details={"tool": tool}
                ) from exc
            return OutboundDispatchResult(
                accepted=bool(response.get("accepted", True)),
                provider_message_id=response.get("provider_message_id"),
                raw=response,
            )

        # Interactive CTA-URL button (WhatsApp): a tappable "Pay QAR 6,000 →"
        # button instead of a raw link. Selected when the outbound message
        # carries a ``cta`` block in its metadata.
        cta = (message.metadata or {}).get("cta") if message.metadata else None
        if message.channel is Channel.WHATSAPP and isinstance(cta, dict) and cta.get("button_url"):
            tool = Tools.EXT_SEND_WHATSAPP_INTERACTIVE
            payload: dict[str, Any] = {
                "to": message.identity,
                "body": message.text or "",
                "buttonText": str(cta.get("button_text") or "Open")[:20],
                "buttonUrl": cta.get("button_url"),
            }
            if cta.get("header"):
                payload["header"] = cta["header"]
            if cta.get("footer"):
                payload["footer"] = cta["footer"]
            try:
                response = await self._tools.call_tool(tool, payload)
            except Exception as exc:  # noqa: BLE001 - normalize transport errors
                raise GatewayError(
                    f"MCP tool {tool!r} failed: {exc}", details={"tool": tool}
                ) from exc
            return OutboundDispatchResult(
                accepted=bool(response.get("accepted", True)),
                provider_message_id=response.get("provider_message_id"),
                raw=response,
            )

        # Interactive reply (quick-reply) buttons (WhatsApp): up to 3 tappable
        # YES/NO-style buttons instead of asking the SME to type. Selected when
        # the outbound carries an ``interactive_buttons`` block with a non-empty
        # ``buttons`` list. The tapped button's title comes back as inbound text.
        ib = (message.metadata or {}).get("interactive_buttons") if message.metadata else None
        if (
            message.channel is Channel.WHATSAPP
            and isinstance(ib, dict)
            and ib.get("buttons")
        ):
            tool = Tools.EXT_SEND_WHATSAPP_INTERACTIVE_BUTTONS
            btn_payload: dict[str, Any] = {
                "to": message.identity,
                "body": message.text or "",
                "buttons": [
                    {"id": str(b.get("id")), "title": str(b.get("title") or "")[:20]}
                    for b in ib["buttons"]
                ][:3],
            }
            if ib.get("header"):
                btn_payload["header"] = ib["header"]
            if ib.get("footer"):
                btn_payload["footer"] = ib["footer"]
            try:
                response = await self._tools.call_tool(tool, btn_payload)
            except Exception as exc:  # noqa: BLE001 - normalize transport errors
                raise GatewayError(
                    f"MCP tool {tool!r} failed: {exc}", details={"tool": tool}
                ) from exc
            return OutboundDispatchResult(
                accepted=bool(response.get("accepted", True)),
                provider_message_id=response.get("provider_message_id"),
                raw=response,
            )

        mapped = _TOOL_BY_CHANNEL.get(message.channel)
        if mapped is None:
            raise GatewayError(
                f"No MCP tool mapped for channel {message.channel}",
                details={"channel": str(message.channel)},
            )
        tool = mapped
        payload = _build_outbound_payload(message.channel, message)
        try:
            response = await self._tools.call_tool(tool, payload)
        except Exception as exc:  # noqa: BLE001 - normalize transport errors
            raise GatewayError(f"MCP tool {tool!r} failed: {exc}", details={"tool": tool}) from exc
        return OutboundDispatchResult(
            accepted=bool(response.get("accepted", True)),
            provider_message_id=response.get("provider_message_id"),
            raw=response,
        )


def _build_outbound_payload(channel: Channel, message: Message) -> dict[str, Any]:
    """Per-tool payload shaping. The UAT WhatsApp tool requires ``to`` +
    ``body``; the email-OTP tool requires ``email``. Other channels'
    payload shapes are added as new tools land."""

    if channel is Channel.WHATSAPP:
        return {"to": message.identity, "body": message.text or ""}
    if channel is Channel.EMAIL:
        # Arbitrary-content email via madad_external_send_email_text: subject +
        # body. Subject comes from the message metadata when the step supplies
        # one, else a sensible default. With CMS-seeded subjects (M1, 2026-06-28)
        # the default should never fire on a templated email — log so the
        # monitor surfaces it for the missing-subject row.
        supplied = (message.metadata or {}).get("subject") if message.metadata else None
        subject = supplied or _DEFAULT_EMAIL_SUBJECT
        if not supplied:
            _LOG.warning(
                "email_send.subject_fallback",
                template=getattr(message, "template_key", None),
                identity=message.identity,
                note="email sent with default subject; CMS template missing data.subject",
            )
        payload: dict[str, Any] = {
            "to": message.identity,
            "subject": str(subject),
            "body_text": message.text or "",
        }
        # Email threading (UAT 2026-06-28). When the conversation already
        # has a root Message-ID, the comms service stamps it on
        # ``metadata.in_reply_to``; pass it through so the email tool
        # writes the ``In-Reply-To`` / ``References`` headers. SME inbox
        # then sees a single thread instead of a disjoint message per
        # outbound. The MCP tool MUST honour these fields; see the
        # Ishan handoff for the contract.
        in_reply_to = (message.metadata or {}).get("in_reply_to") if message.metadata else None
        if in_reply_to:
            payload["in_reply_to"] = str(in_reply_to)
        return payload
    return {"to": message.identity, "body": message.text or ""}
