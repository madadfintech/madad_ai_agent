"""McpMadadIdentityClient — exercises every method against a fake MCP caller."""

from __future__ import annotations

import pytest

from app.services.workflow.mcp_identity import McpMadadIdentityClient
from app.services.workflow.ports import MadadIdentityClient
from app.shared.mcp import InMemoryMCPClient, Tools
from app.shared.workflow.enums import Channel

WA = Channel.WHATSAPP
EMAIL = Channel.EMAIL


def test_mcp_adapter_satisfies_protocol() -> None:
    assert isinstance(
        McpMadadIdentityClient(InMemoryMCPClient()), MadadIdentityClient
    )


# -- open_session: bridge resolves a verified channel identity ----------------


async def test_open_session_existing_user_unwraps_access_token() -> None:
    caller = InMemoryMCPClient(
        handlers={
            Tools.MCP_CREATE_CHANNEL_SESSION: lambda p: {
                "sessionType": "existing_user",
                "accessToken": "AT-123",
                "refreshToken": "RT-456",
                "tokenExpiresAt": 1_700_000_000,
                "userOrLeadRef": "user_42",
            }
        }
    )
    adapter = McpMadadIdentityClient(caller)
    session = await adapter.open_session(channel=WA, identifier="+97455500001")

    assert session.session_type == "existing_user"
    assert session.access_token == "AT-123"
    assert session.refresh_token == "RT-456"
    assert session.token_expires_at == 1_700_000_000
    assert session.user_or_lead_ref == "user_42"
    assert session.onboarding_token is None

    # The bridge tool was called with the right payload (uppercase channel).
    name, payload = caller.calls[0]
    assert name == Tools.MCP_CREATE_CHANNEL_SESSION
    assert payload["channel"] == "WHATSAPP"
    assert payload["identifier"] == "+97455500001"
    assert payload["create_onboarding_token"] is True


async def test_open_session_new_lead_unwraps_onboarding_token() -> None:
    caller = InMemoryMCPClient(
        handlers={
            Tools.MCP_CREATE_CHANNEL_SESSION: lambda p: {
                "sessionType": "new_lead",
                "onboardingToken": "OT-789",
                "userOrLeadRef": "lead_99",
            }
        }
    )
    adapter = McpMadadIdentityClient(caller)
    session = await adapter.open_session(channel=EMAIL, identifier="sme@example.com")

    assert session.session_type == "new_lead"
    assert session.onboarding_token == "OT-789"
    assert session.access_token is None
    assert session.user_or_lead_ref == "lead_99"

    name, payload = caller.calls[0]
    assert payload["channel"] == "EMAIL"


async def test_open_session_omits_unset_optional_params() -> None:
    caller = InMemoryMCPClient(
        handlers={Tools.MCP_CREATE_CHANNEL_SESSION: lambda p: {"sessionType": "new_lead"}}
    )
    adapter = McpMadadIdentityClient(caller)
    await adapter.open_session(channel=WA, identifier="+97499900000")

    _, payload = caller.calls[0]
    # Required args present; unset optionals omitted (cleaner over the wire).
    assert set(payload) == {"channel", "identifier", "create_onboarding_token"}


# -- check_contact: maps domainExists / domain into snake_case fields ---------


async def test_check_contact_known_phone_returns_existing_user_branch() -> None:
    caller = InMemoryMCPClient(
        handlers={
            Tools.AUTH_CHECK_CONTACT: lambda p: {"exists": True, "field": "phone"}
        }
    )
    result = await McpMadadIdentityClient(caller).check_contact(phone="+97455500001")
    assert result.exists is True
    assert result.field == "phone"
    assert result.domain_exists is False


async def test_check_contact_blocked_domain_branch() -> None:
    caller = InMemoryMCPClient(
        handlers={
            Tools.AUTH_CHECK_CONTACT: lambda p: {
                "exists": False,
                "domainExists": True,
                "domain": "acme.com",
            }
        }
    )
    result = await McpMadadIdentityClient(caller).check_contact(
        email="newhire@acme.com"
    )
    assert result.exists is False
    assert result.domain_exists is True
    assert result.domain == "acme.com"


async def test_check_contact_omits_unset_args() -> None:
    caller = InMemoryMCPClient(
        handlers={Tools.AUTH_CHECK_CONTACT: lambda p: {"exists": False}}
    )
    await McpMadadIdentityClient(caller).check_contact(phone="+97499900000")
    _, payload = caller.calls[0]
    assert payload == {"phone": "+97499900000"}


async def test_check_contact_non_qatar_whatsapp_phone_continues_as_new_lead(monkeypatch) -> None:
    monkeypatch.delenv("WORKFLOW__NON_QATAR_WHATSAPP_AUTH_EMAIL", raising=False)
    caller = InMemoryMCPClient(
        handlers={
            Tools.AUTH_CHECK_CONTACT: lambda p: pytest.fail(
                "non-Qatar staging phone should not call backend check-contact"
            )
        }
    )

    result = await McpMadadIdentityClient(caller).check_contact(phone="+918287611995")

    assert result.exists is False
    assert result.domain_exists is False
    assert result.raw["reason"] == "non_qatar_whatsapp_phone"
    assert caller.calls == []


async def test_check_contact_non_qatar_whatsapp_phone_can_use_auth_email_alias(monkeypatch) -> None:
    monkeypatch.setenv(
        "WORKFLOW__NON_QATAR_WHATSAPP_AUTH_EMAIL",
        "tech.external1@madadfintech.com",
    )
    caller = InMemoryMCPClient(
        handlers={
            Tools.AUTH_CHECK_CONTACT: lambda p: {"exists": True, "field": "email"}
        }
    )

    result = await McpMadadIdentityClient(caller).check_contact(phone="+918287611995")

    assert result.exists is True
    assert result.field == "email"
    _, payload = caller.calls[0]
    assert payload == {"email": "tech.external1@madadfintech.com"}


async def test_open_session_non_qatar_whatsapp_can_use_auth_email_alias(monkeypatch) -> None:
    monkeypatch.setenv(
        "WORKFLOW__NON_QATAR_WHATSAPP_AUTH_EMAIL",
        "tech.external1@madadfintech.com",
    )
    caller = InMemoryMCPClient(
        handlers={
            Tools.MCP_CREATE_CHANNEL_SESSION: lambda p: {
                "sessionType": "existing_user",
                "accessToken": "AT-123",
            }
        }
    )

    await McpMadadIdentityClient(caller).open_session(
        channel=WA,
        identifier="+918287611995",
    )

    _, payload = caller.calls[0]
    assert payload["channel"] == "EMAIL"
    assert payload["identifier"] == "tech.external1@madadfintech.com"
    assert payload["email"] == "tech.external1@madadfintech.com"


# -- complete_onboarding ------------------------------------------------------


async def test_complete_onboarding_passes_token_and_returns_raw_dict() -> None:
    caller = InMemoryMCPClient(
        handlers={
            Tools.AUTH_COMPLETE_ONBOARDING: lambda p: {
                "user": {"id": "u-1", "firstName": "Aisha"}
            }
        }
    )
    out = await McpMadadIdentityClient(caller).complete_onboarding(
        first_name="Aisha",
        last_name="Karim",
        onboarding_token="OT-abc",
        phone_number="+97455500099",
    )
    assert out["user"]["id"] == "u-1"
    _, payload = caller.calls[0]
    assert payload["onboarding_token"] == "OT-abc"
    assert payload["first_name"] == "Aisha"
    assert payload["last_name"] == "Karim"
    # UAT schema requires the `phone` arg, not `phone_number`.
    assert payload["phone"] == "+97455500099"


# -- me / refresh / logout ----------------------------------------------------


async def test_me_round_trips_user_object_with_journey_status() -> None:
    caller = InMemoryMCPClient(
        handlers={
            Tools.AUTH_ME: lambda p: {"user": {"id": "u-1", "journeyStatus": "ACTIVATED"}}
        }
    )
    info = await McpMadadIdentityClient(caller).me(access_token="AT-123")
    assert info["user"]["journeyStatus"] == "ACTIVATED"
    _, payload = caller.calls[0]
    assert payload == {"access_token": "AT-123"}


async def test_refresh_accepts_camel_or_snake_response_keys() -> None:
    caller = InMemoryMCPClient(
        handlers={
            Tools.AUTH_REFRESH: lambda p: {
                "accessToken": "AT-new",
                "refreshToken": "RT-new",
                "tokenExpiresAt": 1_800_000_000,
            }
        }
    )
    tokens = await McpMadadIdentityClient(caller).refresh(refresh_token="RT-old")
    assert tokens.access_token == "AT-new"
    assert tokens.refresh_token == "RT-new"
    assert tokens.token_expires_at == 1_800_000_000

    # Now test the snake_case fallback path.
    snake_caller = InMemoryMCPClient(
        handlers={
            Tools.AUTH_REFRESH: lambda p: {
                "access_token": "AT-snake",
                "refresh_token": "RT-snake",
            }
        }
    )
    tokens2 = await McpMadadIdentityClient(snake_caller).refresh(refresh_token="RT-old")
    assert tokens2.access_token == "AT-snake"
    assert tokens2.refresh_token == "RT-snake"


async def test_refresh_without_access_token_raises() -> None:
    caller = InMemoryMCPClient(handlers={Tools.AUTH_REFRESH: lambda p: {}})
    with pytest.raises(ValueError):
        await McpMadadIdentityClient(caller).refresh(refresh_token="RT-old")


async def test_logout_calls_auth_logout_with_access_token() -> None:
    caller = InMemoryMCPClient(handlers={Tools.AUTH_LOGOUT: lambda p: {}})
    await McpMadadIdentityClient(caller).logout(access_token="AT-123")
    name, payload = caller.calls[0]
    assert name == Tools.AUTH_LOGOUT
    assert payload == {"access_token": "AT-123"}
