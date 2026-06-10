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

import hashlib
import os
import re
from datetime import datetime
from typing import Any

from app.shared.mcp import MCPToolCaller, Tools
from app.shared.workflow.enums import Channel

from .ports import AuthTokens, ChannelSession, ContactCheckResult


def _parse_expires_at(value: Any, expires_in_seconds: Any = None) -> int | None:
    """Coerce a session's expiry into a Unix epoch ``int``.

    The cluster now returns ``tokenExpiresAt`` as an ISO-8601 string
    (e.g. ``"2026-06-06T08:03:47.827005Z"``). Older responses only had
    ``expiresInSeconds`` which we add to "now" on this side. Returns
    ``None`` if neither shape is available."""

    if isinstance(value, int):
        return value
    if isinstance(value, str):
        try:
            dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
            return int(dt.timestamp())
        except ValueError:
            pass
    if isinstance(expires_in_seconds, int) and expires_in_seconds > 0:
        return int(datetime.now().timestamp()) + expires_in_seconds
    return None


def _channel_value(channel: Channel) -> str:
    """The MCP bridge tool expects ``WHATSAPP`` / ``EMAIL`` (uppercase)."""

    return channel.value.upper()


def _is_backend_checkable_phone(phone: str) -> bool:
    """Madad's current check-contact endpoint validates Qatar phone numbers.

    Real Qatar WhatsApp users arrive as ``+974XXXXXXXX`` and can be checked.
    Staging/test WhatsApp identities may be non-Qatar (e.g. ``+91...``). Those
    should continue as organic/new leads instead of failing the conversation.
    """

    compact = phone.replace(" ", "").replace("-", "")
    digits = compact[1:] if compact.startswith("+") else compact
    return (digits.startswith("974") and len(digits) == 11) or len(digits) == 8


def _placeholder_email_for_phone(phone: str) -> str:
    """Deterministic, UNIQUE placeholder email for a WhatsApp identity that the
    backend cannot validate as a Qatar phone number.

    Every distinct phone gets its OWN fresh Madad application: the same phone
    always maps to the same email (so repeat messages resume the same user),
    and two different phones never collide. This replaces the previous single
    shared test account (``tech.external1@…`` via
    ``WORKFLOW__NON_QATAR_WHATSAPP_AUTH_EMAIL``) that collapsed every non-Qatar
    WhatsApp lead into one user — obviously wrong once many users onboard.

    The placeholder domain is configurable; the local part encodes the phone so
    it is unique and reproducible.
    """

    digits = re.sub(r"\D", "", phone or "") or "unknown"
    # Parent domain (overridable). Each phone gets its OWN unique SUBDOMAIN under
    # it. The backend guards signup/login per email DOMAIN — it blocks when any
    # user already has ``email endsWith '@' + domain`` (auth.service.ts) — so a
    # SHARED placeholder domain made every WhatsApp lead after the first fail with
    # "domain already registered". The subdomain here is a deterministic hash of
    # the phone: same phone → same email (so check_contact / open_session resume
    # the SAME user and repeat messages don't fork a new account), while two
    # different phones get different, collision-free, random-looking domains.
    # Valid e-mail syntax, and NO backend change required.
    base = (
        os.getenv("WORKFLOW__WHATSAPP_PLACEHOLDER_EMAIL_DOMAIN", "").strip()
        or "madadfintech.com"
    )
    token = hashlib.sha256(digits.encode("utf-8")).hexdigest()[:20]
    return f"wa{digits}@wa{token}.{base}"


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
        create_user_if_missing: bool = False,
    ) -> ChannelSession:
        # When we intend to CREATE the user (WhatsApp organic-entry YES), keep the
        # real WhatsApp phone + channel — do NOT divert to a placeholder email.
        # The backend's create-on-YES (createWhatsappLeadUser) only fires for
        # channel=WHATSAPP and stores the phone as-is for any country, so a
        # non-Qatar number like +91… must NOT be swapped to EMAIL here or no
        # WhatsApp lead is ever created (the user never appears in the portal).
        # The placeholder-email swap stays for the read-only check_contact path
        # where the backend can't yet validate non-Qatar numbers.
        auth_email = (
            _placeholder_email_for_phone(phone or identifier)
            if channel is Channel.WHATSAPP
            and not create_user_if_missing
            and not _is_backend_checkable_phone(identifier)
            else None
        )
        if auth_email:
            channel_value = _channel_value(Channel.EMAIL)
            identifier_value = auth_email
        else:
            channel_value = _channel_value(channel)
            identifier_value = identifier

        payload: dict[str, Any] = {
            "channel": channel_value,
            "identifier": identifier_value,
            "create_onboarding_token": create_onboarding_token,
        }
        # Per Ishan (2026-06-07): set create_user_if_missing=True on the
        # WhatsApp organic-entry call. Backend auto-creates a SIGN_UP account
        # from the phone number alone (no email/password) and returns an
        # accessToken directly — drops the separate complete_onboarding hop.
        # Only forward when True so existing-user resumes don't accidentally
        # toggle the flag on for unrelated paths.
        if create_user_if_missing:
            payload["create_user_if_missing"] = True
        if auth_email:
            payload["email"] = auth_email
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
            token_expires_at=_parse_expires_at(
                response.get("tokenExpiresAt"),
                response.get("expiresInSeconds"),
            ),
            user_or_lead_ref=response.get("userOrLeadRef") or response.get("userId"),
            reference_number=response.get("referenceNumber"),
            raw=response,
        )

    async def check_contact(
        self, *, phone: str | None = None, email: str | None = None
    ) -> ContactCheckResult:
        if phone is not None and email is None and not _is_backend_checkable_phone(phone):
            # Non-Qatar WhatsApp phone: check the per-phone placeholder identity
            # so a returning lead resolves to its own existing user (and a first
            # contact reads as new) — instead of a shared test account.
            email = _placeholder_email_for_phone(phone)
            phone = None

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

    async def update_onboarding_progress(
        self,
        *,
        user_id: str | None = None,
        channel: Channel | None = None,
        identifier: str | None = None,
        step: int | None = None,
        touch_inbound: bool = False,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {}
        if user_id is not None:
            payload["user_id"] = user_id
        if channel is not None:
            payload["channel"] = _channel_value(channel)
        if identifier is not None:
            payload["identifier"] = identifier
        if step is not None:
            payload["step"] = step
        if touch_inbound:
            payload["touch_inbound"] = True
        return await self._tools.call_tool(
            Tools.MCP_UPDATE_ONBOARDING_PROGRESS, payload
        )

    async def get_onboarding_progress(
        self,
        *,
        user_id: str | None = None,
        channel: Channel | None = None,
        identifier: str | None = None,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {}
        if user_id is not None:
            payload["user_id"] = user_id
        if channel is not None:
            payload["channel"] = _channel_value(channel)
        if identifier is not None:
            payload["identifier"] = identifier
        return await self._tools.call_tool(
            Tools.MCP_GET_ONBOARDING_PROGRESS, payload
        )

    async def check_registration(
        self,
        *,
        identifier: str,
        channel: Channel,
        email: str | None = None,
        phone: str | None = None,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "identifier": identifier,
            "channel": _channel_value(channel),
        }
        if email is not None:
            payload["email"] = email
        if phone is not None:
            payload["phone"] = phone
        return await self._tools.call_tool(
            Tools.MCP_CHECK_REGISTRATION, payload
        )
