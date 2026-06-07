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

from pydantic import BaseModel

from app.shared.mcp import MCPToolCaller, Tools
from app.shared.workflow.enums import Channel
from app.shared.workflow.utils import new_id

from .errors import GatewayError
from .models import Message


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
    Channel.EMAIL: Tools.EXT_SEND_EMAIL_OTP,
}


class McpCommunicationGateway(CommunicationGateway):
    """Dispatches outbound messages by calling MCP tools."""

    def __init__(self, tool_caller: MCPToolCaller) -> None:
        self._tools = tool_caller

    async def send(self, message: Message) -> OutboundDispatchResult:
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

        tool = _TOOL_BY_CHANNEL.get(message.channel)
        if tool is None:
            raise GatewayError(
                f"No MCP tool mapped for channel {message.channel}",
                details={"channel": str(message.channel)},
            )
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
        # The current EMAIL mapping is to EXT_SEND_EMAIL_OTP — a
        # send-an-OTP tool, NOT arbitrary-email-content. The body is
        # discarded by the backend; only the recipient address is used.
        return {"email": message.identity}
    return {"to": message.identity, "body": message.text or ""}
