"""MCP-backed implementation of :class:`MadadIdentityClient`.

Wraps the six identity tools defined in Ishan's MCP cluster:

* ``madad_mcp_create_channel_session`` — the bridge that turns a verified
  WhatsApp/email identity into a scoped session token. THIS is the agent's
  authentication entry, NOT the portal-style OTP flow.
* ``madad_auth_check_contact`` — the Q8 three-way (existing user / new
  contact / domain-blocked).
* ``madad_auth_complete_onboarding`` — finalises a new-lead session and
  creates the Madad user record.
* ``madad_auth_me`` — returns the current user (including ``journeyStatus``).
  Used as the polling backstop until Ishan's backend webhook emission ships.
* ``madad_auth_refresh`` — exchanges a refresh token for a fresh access token.
* ``madad_auth_logout`` — revokes the current session.

The MCP tool *arguments* are snake_case (Python-native, as defined in Ishan's
``fastmcp_tools/`` modules). The tool *response payloads* are camelCase
(backend REST style), so the adapter performs the small field-rename when
materialising :class:`ChannelSession`, :class:`ContactCheckResult` and
:class:`AuthTokens`.
"""

from __future__ import annotations

from typing import Any

from app.shared.mcp import MCPToolCaller, Tools
from app.shared.workflow.enums import Channel

from .ports import AuthTokens, ChannelSession, ContactCheckResult


def _channel_value(channel: Channel) -> str:
    """The MCP bridge tool expects ``WHATSAPP`` / ``EMAIL`` (uppercase)."""

    return channel.value.upper()


class McpMadadIdentityClient:
    """MCP-backed implementation of the :class:`MadadIdentityClient` port."""

    def __init__(self, tool_caller: MCPToolCaller) -> None:
        self._tools = tool_caller

    async def open_session(
        self,
        *,
        channel: Channel,
        identifier: str,
        email: str | None = None,
        phone: str | None = None,
        display_name: str | None = None,
        create_onboarding_token: bool = True,
    ) -> ChannelSession:
        payload: dict[str, Any] = {
            "channel": _channel_value(channel),
            "identifier": identifier,
            "create_onboarding_token": create_onboarding_token,
        }
        if email is not None:
            payload["email"] = email
        if phone is not None:
            payload["phone"] = phone
        if display_name is not None:
            payload["display_name"] = display_name

        response = await self._tools.call_tool(Tools.MCP_CREATE_CHANNEL_SESSION, payload)
        # Backend returns camelCase ``sessionType`` / ``accessToken`` / etc.
        return ChannelSession(
            session_type=response.get("sessionType", "new_lead"),
            access_token=response.get("accessToken"),
            onboarding_token=response.get("onboardingToken"),
            refresh_token=response.get("refreshToken"),
            token_expires_at=response.get("tokenExpiresAt"),
            user_or_lead_ref=response.get("userOrLeadRef"),
            raw=response,
        )

    async def check_contact(
        self, *, phone: str | None = None, email: str | None = None
    ) -> ContactCheckResult:
        payload: dict[str, Any] = {}
        if phone is not None:
            payload["phone"] = phone
        if email is not None:
            payload["email"] = email

        response = await self._tools.call_tool(Tools.AUTH_CHECK_CONTACT, payload)
        return ContactCheckResult(
            exists=bool(response.get("exists", False)),
            field=response.get("field"),
            domain_exists=bool(response.get("domainExists", False)),
            domain=response.get("domain"),
            raw=response,
        )

    async def complete_onboarding(
        self,
        *,
        first_name: str,
        last_name: str,
        onboarding_token: str,
        email: str | None = None,
        phone_number: str | None = None,
        legal_entity_name: str | None = None,
        cr_number: str | None = None,
        is_qatar_based: bool | None = None,
        role: str | None = None,
    ) -> dict[str, Any]:
        # UAT cluster requires ALL of: first_name, last_name,
        # legal_entity_name, cr_number, is_qatar_based, email, phone, role,
        # onboarding_token. Phase 2's collect-onboarding-details node only
        # asked for first/last name; the new fields are gathered by the
        # extended intake form (Phase 2.x staging update) and pass through
        # here. Optional in the signature so the InMemoryMadadIdentityClient
        # tests stay green; the cluster will 400 if any is missing.
        payload: dict[str, Any] = {
            "first_name": first_name,
            "last_name": last_name,
            "onboarding_token": onboarding_token,
        }
        if email is not None:
            payload["email"] = email
        if phone_number is not None:
            payload["phone"] = phone_number
        if legal_entity_name is not None:
            payload["legal_entity_name"] = legal_entity_name
        if cr_number is not None:
            payload["cr_number"] = cr_number
        if is_qatar_based is not None:
            payload["is_qatar_based"] = is_qatar_based
        if role is not None:
            payload["role"] = role
        return await self._tools.call_tool(Tools.AUTH_COMPLETE_ONBOARDING, payload)

    async def me(self, *, access_token: str) -> dict[str, Any]:
        response = await self._tools.call_tool(
            Tools.AUTH_ME, {"access_token": access_token}
        )
        # UAT returns user fields at the TOP level (no {user: {...}} wrapper).
        # Re-shape so callers can read journey_status uniformly across the
        # in-memory fake (which returns the nested shape) and production.
        if isinstance(response, dict) and "user" not in response and (
            "journeyStatus" in response or "journey_status" in response
        ):
            return {"user": response}
        return response

    async def refresh(self, *, refresh_token: str) -> AuthTokens:
        response = await self._tools.call_tool(
            Tools.AUTH_REFRESH, {"refresh_token": refresh_token}
        )
        # Backend may use either casing on refresh responses depending on which
        # service handler responded; accept both for forward-compat.
        access = response.get("accessToken") or response.get("access_token")
        if not isinstance(access, str):
            raise ValueError("AUTH_REFRESH response missing accessToken")
        new_refresh = response.get("refreshToken") or response.get("refresh_token")
        expires = response.get("tokenExpiresAt") or response.get("token_expires_at")
        return AuthTokens(
            access_token=access,
            refresh_token=new_refresh,
            token_expires_at=expires,
            raw=response,
        )

    async def logout(self, *, access_token: str) -> None:
        await self._tools.call_tool(
            Tools.AUTH_LOGOUT, {"access_token": access_token}
        )
