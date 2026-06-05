"""McpCommunicationGateway routes sends to the right MCP tools (via fake caller)."""

from __future__ import annotations

import pytest

from app.core.config import McpSettings
from app.services.communication.enums import MessageDirection
from app.services.communication.errors import GatewayError
from app.services.communication.gateway import McpCommunicationGateway
from app.services.communication.models import Message
from app.shared.mcp import InMemoryMCPClient, Tools
from app.shared.workflow.enums import Channel


def _msg(channel: Channel) -> Message:
    return Message(
        conversation_id="c1",
        channel=channel,
        identity="+97455500001" if channel is Channel.WHATSAPP else "sme@example.com",
        direction=MessageDirection.OUTBOUND,
        text="hello",
    )


async def test_whatsapp_routes_to_whatsapp_text_tool():
    caller = InMemoryMCPClient(
        handlers={
            Tools.EXT_SEND_WHATSAPP_TEXT: lambda p: {
                "accepted": True,
                "provider_message_id": "wa1",
            }
        }
    )
    gateway = McpCommunicationGateway(caller)

    result = await gateway.send(_msg(Channel.WHATSAPP))

    assert caller.calls[0][0] == Tools.EXT_SEND_WHATSAPP_TEXT
    assert caller.calls[0][1]["to"] == "+97455500001"
    # UAT EXT_SEND_WHATSAPP_TEXT expects `body`, not `text`.
    assert caller.calls[0][1]["body"] == "hello"
    assert result.accepted is True
    assert result.provider_message_id == "wa1"


async def test_email_routes_to_email_otp_tool():
    # NB: the cluster only exposes an OTP-only email tool today (no arbitrary
    # email send). The gateway routes Channel.EMAIL there for now; Phase 2 will
    # split OTP-style vs informational sends per Q2.
    caller = InMemoryMCPClient(handlers={Tools.EXT_SEND_EMAIL_OTP: lambda p: {"accepted": True}})
    gateway = McpCommunicationGateway(caller)

    await gateway.send(_msg(Channel.EMAIL))
    assert caller.calls[0][0] == Tools.EXT_SEND_EMAIL_OTP


async def test_transport_failure_is_normalised_to_gateway_error():
    caller = InMemoryMCPClient(fail_times=1, settings=McpSettings(retry_max_attempts=1))
    gateway = McpCommunicationGateway(caller)

    with pytest.raises(GatewayError):
        await gateway.send(_msg(Channel.WHATSAPP))
