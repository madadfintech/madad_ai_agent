"""Business ports the onboarding workflow orchestrates through.

The workflow nodes never touch external systems directly — they call these
ports. Real adapters bridge to the Communication and Nudge services (and, via
MCP, to Madad's identity + KYC backend); in-memory fakes drive deterministic
tests.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any, Literal, Protocol, runtime_checkable

from pydantic import BaseModel, Field

from app.shared.workflow.enums import Channel
from app.shared.workflow.utils import new_id

SessionType = Literal[
    "existing_user",
    "new_lead",
    # Per Ishan (2026-06-07): backend returns this when
    # ``create_user_if_missing=True`` minted a fresh SIGN_UP account on the
    # spot (WhatsApp organic-entry path).
    "new_user_created",
    # Per Ishan: backend returns this when a portal user already exists for
    # the phone — agent should tell them to log in to madadfintech.com.
    "existing_portal_user",
]
ContactField = Literal["phone", "email"]

# -- Messenger (outbound conversation via Communication + CMS) ---------------


class Messenger(ABC):
    @abstractmethod
    async def send(
        self,
        *,
        channel: Channel,
        identity: str,
        template_key: str,
        variables: dict[str, Any] | None = None,
        locale: str = "en",
    ) -> None: ...

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
        """Send an interactive CTA-URL button (body rendered from
        ``template_key``). Returns ``True`` if it was dispatched as a button.

        Default implementation declines (``False``) so callers fall back to a
        plain-text send; real messengers override this.
        """

        return False

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
        """Send a pre-approved WhatsApp TEMPLATE by name (e.g. the "initiate"
        reach-out). Required for the FIRST outbound to a contact and any
        message outside Meta's 24h window, where free text is rejected.
        ``template_key`` is the CMS key mirroring the template body (for the
        comms record). Returns ``True`` if dispatched. Default declines; real
        messengers override.
        """

        return False


class RecordingMessenger(Messenger):
    """Records outbound sends (no rendering needed) for tests."""

    def __init__(self) -> None:
        self.sent: list[dict[str, Any]] = []

    async def send(
        self,
        *,
        channel: Channel,
        identity: str,
        template_key: str,
        variables: dict[str, Any] | None = None,
        locale: str = "en",
    ) -> None:
        self.sent.append(
            {
                "channel": channel,
                "identity": identity,
                "template_key": template_key,
                "variables": variables or {},
                "locale": locale,
            }
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
        self.sent.append(
            {
                "channel": channel,
                "identity": identity,
                "template_key": template_key,
                "variables": variables or {},
                "locale": locale,
                "cta": {"button_text": button_text, "button_url": button_url},
            }
        )
        return True

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
        self.sent.append(
            {
                "channel": channel,
                "identity": identity,
                "template_key": template_key,
                "variables": variables or {},
                # Mirror the regular send()'s ``locale`` key so
                # test_locale_propagates and other locale-aware tests can
                # inspect the dispatched locale without forking the assertion
                # by send-shape.
                "locale": language_code,
                "whatsapp_template": {
                    "name": template_name,
                    "language": language_code,
                    "components": components or [],
                },
            }
        )
        return True

    def templates(self) -> list[str]:
        return [s["template_key"] for s in self.sent if "template_key" in s]


# -- Models for identity / channel session responses --------------------------


class ChannelSession(BaseModel):
    """Result of ``madad_mcp_create_channel_session`` for one channel identity.

    Both tokens are optional because the bridge tool returns either an
    ``access_token`` (existing_user) or an ``onboarding_token`` (new_lead).
    ``raw`` preserves the full backend response for audit / debugging.
    """

    session_type: SessionType
    access_token: str | None = None
    onboarding_token: str | None = None
    refresh_token: str | None = None
    token_expires_at: int | None = None  # unix epoch seconds when known
    user_or_lead_ref: str | None = None
    # Per Ishan (2026-06-07): the channel-session response also carries
    # ``referenceNumber`` (= ``User.uniqueId`` on the backend) which is the
    # user-facing application reference (e.g. ``Y6NICTES``, 8-char uppercase
    # alphanumeric — NOT the 7-digit format the PDF mock shows). Populates
    # immediately on account creation.
    reference_number: str | None = None
    raw: dict[str, Any] = Field(default_factory=dict)


class ContactCheckResult(BaseModel):
    """Result of ``madad_auth_check_contact`` — three-way branch per Q8.

    * ``exists=True`` + ``field`` → known user; resume / login path.
    * ``exists=False, domain_exists=False`` → fresh signup is OK.
    * ``exists=False, domain_exists=True, domain`` → blocked: this email domain
      belongs to another business; route to support.
    """

    exists: bool
    field: ContactField | None = None
    domain_exists: bool = False
    domain: str | None = None
    raw: dict[str, Any] = Field(default_factory=dict)


class AuthTokens(BaseModel):
    """Token pair returned by ``madad_auth_refresh``."""

    access_token: str
    refresh_token: str | None = None
    token_expires_at: int | None = None
    raw: dict[str, Any] = Field(default_factory=dict)


# -- MadadIdentityClient: the new port the channel-session + auth flow uses ---


@runtime_checkable
class MadadIdentityClient(Protocol):
    """Identity-side Madad operations the workflow needs.

    Implementations: ``InMemoryMadadIdentityClient`` (tests) and
    ``McpMadadIdentityClient`` (production — wraps the matching MCP tools).
    Replaces the fabricated ``MadadClient`` port; Phase 2 of the integration
    plan reshapes the onboarding graph onto it.
    """

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
    ) -> ChannelSession: ...

    async def check_contact(
        self, *, phone: str | None = None, email: str | None = None
    ) -> ContactCheckResult: ...

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
    ) -> dict[str, Any]: ...

    async def me(self, *, access_token: str) -> dict[str, Any]: ...

    async def refresh(self, *, refresh_token: str) -> AuthTokens: ...

    async def logout(self, *, access_token: str) -> None: ...

    async def update_onboarding_progress(
        self,
        *,
        user_id: str | None = None,
        channel: Channel | None = None,
        identifier: str | None = None,
        step: int | None = None,
        touch_inbound: bool = False,
    ) -> dict[str, Any]:
        """Per Ishan (2026-06-07): record a WhatsApp lead's onboarding step.

        Backend hard-gates the pre-qualified document checklist on ``step >= 3``,
        so the agent MUST call this with ``step=3`` after the audited financial
        report upload + account-created message fires. ``touch_inbound=True`` on
        every inbound keeps Meta's 24h window fresh. Identify the lead by
        ``user_id``, OR by ``channel + identifier`` (phone).
        """
        ...

    async def get_onboarding_progress(
        self,
        *,
        user_id: str | None = None,
        channel: Channel | None = None,
        identifier: str | None = None,
    ) -> dict[str, Any]:
        """Read the current onboarding step / last_inbound_at / journey_status
        for a WhatsApp lead. Used for resume logic when the workflow loses its
        own in-memory state."""
        ...

    async def set_business_email(
        self,
        *,
        email: str,
        user_id: str | None = None,
        channel: Channel | None = None,
        identifier: str | None = None,
    ) -> dict[str, Any]:
        """Attach the lead's BUSINESS email (step right after YES). Returns
        ``{ok, conflict, alreadyPortalUser, ...}`` — ``conflict=True`` means the
        email is already registered, so ask for a different email / contact
        support instead of advancing to the CR step."""
        ...

    async def check_registration(
        self,
        *,
        identifier: str,
        channel: Channel,
        email: str | None = None,
        phone: str | None = None,
    ) -> dict[str, Any]:
        """Per Ishan (cluster commit e6ea5d2, 2026-06-10): read-only
        registration lookup. Returns ``{registered, route, ...}``. When
        ``registered`` is True, ``route`` tells the workflow which
        downstream branch to take instead of starting fresh onboarding.

        Routes (see project_check_registration_tool memory for details):
          * portal_login_required, invoice_discounting,
            offer_accepted_confirmation, offers_available,
            payment_received, payment_link, continue_step.

        Plus payload: ``whatsappOnboardingStep``, ``journeyStatus``,
        ``onboardingFeePaid``, ``creditLineActive``, ``creditLine{...}``,
        ``offerAccepted``, ``hasPendingOffers``, ``offers[...]``,
        ``userId``, ``referenceNumber``.

        Returns ``{registered: False}`` when no account matches —
        caller proceeds with the normal SIGN_UP path.
        """
        ...


class InMemoryMadadIdentityClient:
    """Configurable fake implementing :class:`MadadIdentityClient`.

    Tests seed ``known_phones`` / ``known_emails`` to simulate existing users,
    and ``blocked_domains`` to simulate the third Q8 branch (domain belongs to
    another business). The default ``journey_status`` is what ``me()`` returns
    until the test overrides it. Every call is captured in ``calls`` for
    introspection.
    """

    def __init__(
        self,
        *,
        known_phones: dict[str, str] | None = None,
        known_emails: dict[str, str] | None = None,
        blocked_domains: dict[str, str] | None = None,
        journey_status: str = "ONBOARDED",
    ) -> None:
        # phone / email → user id (the SME the backend resolved them to)
        self._users_by_phone: dict[str, str] = dict(known_phones or {})
        self._users_by_email: dict[str, str] = dict(known_emails or {})
        # email host → owning organisation name (for the blocked branch)
        self._blocked_domains: dict[str, str] = dict(blocked_domains or {})
        self.journey_status: str = journey_status
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self._revoked_tokens: set[str] = set()

    def _record(self, name: str, **kwargs: Any) -> None:
        self.calls.append((name, kwargs))

    def _user_for(self, *, phone: str | None, email: str | None) -> str | None:
        if phone is not None and phone in self._users_by_phone:
            return self._users_by_phone[phone]
        if email is not None and email in self._users_by_email:
            return self._users_by_email[email]
        return None

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
        self._record(
            "open_session",
            channel=channel,
            identifier=identifier,
            email=email,
            phone=phone,
            display_name=display_name,
            create_onboarding_token=create_onboarding_token,
            create_user_if_missing=create_user_if_missing,
        )
        # The MCP bridge tool resolves identifier-on-channel — the in-memory
        # equivalent looks up whichever index matches the channel.
        match_phone = identifier if channel is Channel.WHATSAPP else phone
        match_email = identifier if channel is Channel.EMAIL else email
        user_id = self._user_for(phone=match_phone, email=match_email)
        if user_id is not None:
            return ChannelSession(
                session_type="existing_user",
                access_token=new_id("at"),
                refresh_token=new_id("rt"),
                user_or_lead_ref=user_id,
                raw={"identifier": identifier, "channel": str(channel)},
            )
        # Per Ishan (2026-06-07): when create_user_if_missing=True (the
        # WhatsApp organic-entry path), the backend auto-creates a SIGN_UP
        # account from the phone number alone and returns an accessToken
        # directly — no separate complete_onboarding round-trip needed.
        # Simulate that here so the in-memory tests cover the new branch.
        if create_user_if_missing:
            return ChannelSession(
                session_type="new_user_created",
                access_token=new_id("at"),
                refresh_token=new_id("rt"),
                user_or_lead_ref=new_id("user"),
                reference_number=new_id("ref")[-8:].upper(),
                raw={"identifier": identifier, "channel": str(channel)},
            )
        return ChannelSession(
            session_type="new_lead",
            onboarding_token=new_id("ot") if create_onboarding_token else None,
            user_or_lead_ref=new_id("lead"),
            raw={"identifier": identifier, "channel": str(channel)},
        )

    async def check_contact(
        self, *, phone: str | None = None, email: str | None = None
    ) -> ContactCheckResult:
        self._record("check_contact", phone=phone, email=email)
        if phone is not None and phone in self._users_by_phone:
            return ContactCheckResult(exists=True, field="phone")
        if email is not None and email in self._users_by_email:
            return ContactCheckResult(exists=True, field="email")
        if email is not None and "@" in email:
            domain = email.rsplit("@", 1)[1]
            if domain in self._blocked_domains:
                return ContactCheckResult(
                    exists=False, domain_exists=True, domain=domain
                )
        return ContactCheckResult(exists=False, domain_exists=False)

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
        self._record(
            "complete_onboarding",
            first_name=first_name,
            last_name=last_name,
            onboarding_token=onboarding_token,
            email=email,
            phone_number=phone_number,
            legal_entity_name=legal_entity_name,
            cr_number=cr_number,
            is_qatar_based=is_qatar_based,
            role=role,
        )
        user_id = new_id("usr")
        if phone_number is not None:
            self._users_by_phone[phone_number] = user_id
        if email is not None:
            self._users_by_email[email] = user_id
        return {
            "user": {"id": user_id, "firstName": first_name, "lastName": last_name},
            "raw": {"onboarding_token": onboarding_token},
        }

    async def me(self, *, access_token: str) -> dict[str, Any]:
        self._record("me", access_token=access_token)
        if access_token in self._revoked_tokens:
            raise RuntimeError("access token revoked")
        return {"user": {"journeyStatus": self.journey_status}}

    async def refresh(self, *, refresh_token: str) -> AuthTokens:
        self._record("refresh", refresh_token=refresh_token)
        return AuthTokens(access_token=new_id("at"), refresh_token=new_id("rt"))

    async def logout(self, *, access_token: str) -> None:
        self._record("logout", access_token=access_token)
        self._revoked_tokens.add(access_token)

    async def update_onboarding_progress(
        self,
        *,
        user_id: str | None = None,
        channel: Channel | None = None,
        identifier: str | None = None,
        step: int | None = None,
        touch_inbound: bool = False,
    ) -> dict[str, Any]:
        self._record(
            "update_onboarding_progress",
            user_id=user_id,
            channel=channel,
            identifier=identifier,
            step=step,
            touch_inbound=touch_inbound,
        )
        return {"step": step, "touch_inbound": touch_inbound, "user_id": user_id}

    async def get_onboarding_progress(
        self,
        *,
        user_id: str | None = None,
        channel: Channel | None = None,
        identifier: str | None = None,
    ) -> dict[str, Any]:
        self._record(
            "get_onboarding_progress",
            user_id=user_id,
            channel=channel,
            identifier=identifier,
        )
        return {"step": 0, "journey_status": "SIGN_UP", "last_inbound_at": None}

    async def set_business_email(
        self,
        *,
        email: str,
        user_id: str | None = None,
        channel: Channel | None = None,
        identifier: str | None = None,
    ) -> dict[str, Any]:
        self._record(
            "set_business_email",
            email=email,
            user_id=user_id,
            channel=channel,
            identifier=identifier,
        )
        # Configurable conflict for tests: emails in ``self.taken_emails`` clash.
        conflict = email.strip().lower() in getattr(self, "taken_emails", set())
        return {"ok": not conflict, "conflict": conflict, "email": email}

    async def check_registration(
        self,
        *,
        identifier: str,
        channel: Channel,
        email: str | None = None,
        phone: str | None = None,
    ) -> dict[str, Any]:
        self._record(
            "check_registration",
            identifier=identifier,
            channel=channel,
            email=email,
            phone=phone,
        )
        # Opt-in registered response: most tests only set ``known_phones``
        # to exercise the SIGN_UP → SIGN_UP-with-existing-user path of
        # ``check_contact`` / ``open_session``. Returning-user behaviour
        # is gated on an explicit ``check_registration_overrides`` entry
        # so those tests don't accidentally trigger the new
        # ``registered_route_send`` branch.
        override = getattr(self, "check_registration_overrides", {}).get(identifier)
        if override is None:
            return {"registered": False}
        match_phone = identifier if channel is Channel.WHATSAPP else phone
        match_email = identifier if channel is Channel.EMAIL else email
        user_id = self._user_for(phone=match_phone, email=match_email)
        base: dict[str, Any] = {
            "registered": True,
            "userId": user_id or override.get("userId"),
            "route": "continue_step",
            "whatsappOnboarding": True,
            "whatsappOnboardingStep": 0,
            "journeyStatus": self.journey_status,
            "onboardingFeePaid": False,
            "creditLineActive": False,
            "offerAccepted": False,
            "hasPendingOffers": False,
            "offers": [],
        }
        return {**base, **override}


# -- KycClient: the new port the Phase 2 graph uses for KYC tools -------------


@runtime_checkable
class KycClient(Protocol):
    """KYC-side Madad operations the onboarding graph drives.

    Implementations: ``InMemoryKycClient`` (tests) and ``McpKycClient``
    (production — wraps ``madad_kyc_*`` MCP tools). The Phase 2 onboarding
    reshape replaces ``MadadClient.check_eligibility`` / ``request_score`` /
    ``submit_to_lenders`` / ``activate_credit_line`` (none of which exist in
    the real catalog) with these calls into the actual KYC backend.

    All calls take ``access_token`` per-call rather than holding it as instance
    state — different workflow runs may have different tokens, and the workflow
    state is the source of truth.
    """

    async def upload_commercial_registration(
        self,
        *,
        access_token: str,
        content_base64: str,
        filename: str,
        mime_type: str | None = None,
    ) -> dict[str, Any]: ...

    async def update_eligibility(
        self, *, access_token: str, data: dict[str, Any]
    ) -> dict[str, Any]: ...

    async def upload_audited_financial_report(
        self,
        *,
        access_token: str,
        content_base64: str,
        filename: str,
        mime_type: str | None = None,
    ) -> dict[str, Any]: ...

    async def get_admin_requested_documents(
        self, *, access_token: str
    ) -> dict[str, Any]: ...

    async def upload_document_base64(
        self,
        *,
        access_token: str,
        content_base64: str,
        filename: str,
        document_type: str,
        mime_type: str | None = None,
    ) -> dict[str, Any]: ...

    async def classify_and_upload_document_base64(
        self,
        *,
        access_token: str,
        content_base64: str,
        filename: str,
        mime_type: str | None = None,
        document_param: str | None = None,
        document_label: str | None = None,
    ) -> dict[str, Any]:
        """Per Ishan (2026-06-07): preferred upload tool for WhatsApp/email
        attachments where the SME hasn't told us what doc type they're
        sending. The backend classifier (same one the MSME portal uses)
        decides the type and routes to the right entity slot; the response
        carries the resolved ``document_type`` so the agent can acknowledge
        per-doc without filename guessing on our side.
        """
        ...

    async def classify_and_upload_zip_base64(
        self,
        *,
        access_token: str,
        content_base64: str,
        filename: str,
        continue_on_error: bool = True,
    ) -> dict[str, Any]:
        """Expand a ZIP and classify + upload every member through the
        portal pipeline. Returns the per-file checklist (file_name,
        resolved document_type, confidently_classified bool) — exactly the
        shape needed to render the PDF Step 4 'ZIP received / found these /
        still missing X' message."""
        ...

    async def upload_invoice_base64(
        self,
        *,
        access_token: str,
        content_base64: str,
        filename: str,
        mime_type: str | None = None,
    ) -> dict[str, Any]:
        """Submit an invoice for financing from base64 (ACTIVE SME uploads an
        invoice PDF). Backend reuses the portal extraction + submission path."""
        ...

    async def add_buyer(
        self, *, access_token: str, data: dict[str, Any]
    ) -> dict[str, Any]: ...

    async def add_shareholders(
        self, *, access_token: str, shareholders: list[dict[str, Any]]
    ) -> dict[str, Any]: ...


class InMemoryKycClient:
    """Configurable fake implementing :class:`KycClient`.

    Tests seed ``required_documents`` to drive the documents-loop missing check.
    Every call is captured in ``calls`` for introspection; uploaded documents
    accumulate in ``uploaded_documents`` (keyed by document_type) so the
    documents-loop converges as a real flow would.
    """

    def __init__(
        self,
        *,
        required_documents: list[str] | None = None,
        eligibility_result: dict[str, Any] | None = None,
    ) -> None:
        self._required_documents: list[str] = list(required_documents or [])
        self._eligibility_result: dict[str, Any] = dict(
            eligibility_result or {"status": "submitted"}
        )
        self.uploaded_documents: dict[str, dict[str, Any]] = {}
        self.cr_document: dict[str, Any] | None = None
        self.financial_report: dict[str, Any] | None = None
        self.buyers: list[dict[str, Any]] = []
        self.shareholders: list[dict[str, Any]] = []
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def _record(self, name: str, **kwargs: Any) -> None:
        self.calls.append((name, kwargs))

    async def upload_commercial_registration(
        self,
        *,
        access_token: str,
        content_base64: str,
        filename: str,
        mime_type: str | None = None,
    ) -> dict[str, Any]:
        self._record(
            "upload_commercial_registration",
            access_token=access_token,
            filename=filename,
            mime_type=mime_type,
        )
        self.cr_document = {
            "filename": filename, "content_base64": content_base64, "mime_type": mime_type,
        }
        return {"document_id": new_id("cr"), "filename": filename}

    async def update_eligibility(
        self, *, access_token: str, data: dict[str, Any]
    ) -> dict[str, Any]:
        self._record("update_eligibility", access_token=access_token, data=data)
        return self._eligibility_result

    async def upload_audited_financial_report(
        self,
        *,
        access_token: str,
        content_base64: str,
        filename: str,
        mime_type: str | None = None,
    ) -> dict[str, Any]:
        self._record(
            "upload_audited_financial_report",
            access_token=access_token,
            filename=filename,
            mime_type=mime_type,
        )
        self.financial_report = {
            "filename": filename, "content_base64": content_base64, "mime_type": mime_type,
        }
        return {"document_id": new_id("fr"), "filename": filename}

    async def get_admin_requested_documents(
        self, *, access_token: str
    ) -> dict[str, Any]:
        self._record("get_admin_requested_documents", access_token=access_token)
        missing = [
            code for code in self._required_documents if code not in self.uploaded_documents
        ]
        return {"required": list(self._required_documents), "missing": missing}

    async def upload_document_base64(
        self,
        *,
        access_token: str,
        content_base64: str,
        filename: str,
        document_type: str,
        mime_type: str | None = None,
    ) -> dict[str, Any]:
        self._record(
            "upload_document_base64",
            access_token=access_token,
            filename=filename,
            document_type=document_type,
            mime_type=mime_type,
        )
        self.uploaded_documents[document_type] = {
            "filename": filename,
            "content_base64": content_base64,
            "mime_type": mime_type,
        }
        return {"document_id": new_id("doc"), "document_type": document_type}

    async def classify_and_upload_document_base64(
        self,
        *,
        access_token: str,
        content_base64: str,
        filename: str,
        mime_type: str | None = None,
        document_param: str | None = None,
        document_label: str | None = None,
    ) -> dict[str, Any]:
        """In-memory classifier: looks at the filename keywords to pick a
        type (mirrors the workflow's old _infer_doc_type), defaults to
        ``ADDITIONAL_DOCUMENT`` so files are never lost."""
        self._record(
            "classify_and_upload_document_base64",
            access_token=access_token,
            filename=filename,
            mime_type=mime_type,
            document_param=document_param,
            document_label=document_label,
        )
        # Tiny stand-in classifier: filename keywords decide.
        lowered = (filename or "").lower()
        keyword_map = {
            "cr": "commercial_registration",
            "commercial": "commercial_registration",
            "audited": "audited_report",
            "financial": "audited_report",
            "trade": "trade_license",
            "tax": "tax_card",
            "bank": "bank_statement",
            "vat": "vat_certificate",
            "establishment": "establishment_card",
            "qid": "qid",
            "passport": "passport",
        }
        document_type = "additional_document"
        for kw, dt in keyword_map.items():
            if kw in lowered:
                document_type = dt
                break
        self.uploaded_documents[document_type] = {
            "filename": filename,
            "content_base64": content_base64,
            "mime_type": mime_type,
        }
        return {
            "document_id": new_id("doc"),
            "document_type": document_type,
            "confidently_classified": document_type != "additional_document",
        }

    async def classify_and_upload_zip_base64(
        self,
        *,
        access_token: str,
        content_base64: str,
        filename: str,
        continue_on_error: bool = True,
    ) -> dict[str, Any]:
        """In-memory ZIP classify-and-upload — empty checklist by default;
        tests can preload self.zip_contents to model contents."""
        self._record(
            "classify_and_upload_zip_base64",
            access_token=access_token,
            filename=filename,
            continue_on_error=continue_on_error,
        )
        return {"checklist": [], "uploaded_count": 0}

    async def upload_invoice_base64(
        self,
        *,
        access_token: str,
        content_base64: str,
        filename: str,
        mime_type: str | None = None,
    ) -> dict[str, Any]:
        self._record(
            "upload_invoice_base64",
            access_token=access_token,
            filename=filename,
            mime_type=mime_type,
        )
        return {"success": True, "data": {"invoiceNumber": filename}}

    async def add_buyer(
        self, *, access_token: str, data: dict[str, Any]
    ) -> dict[str, Any]:
        self._record("add_buyer", access_token=access_token, data=data)
        record = {"buyer_id": new_id("buyer"), **data}
        self.buyers.append(record)
        return record

    async def add_shareholders(
        self, *, access_token: str, shareholders: list[dict[str, Any]]
    ) -> dict[str, Any]:
        self._record(
            "add_shareholders", access_token=access_token, shareholders=shareholders
        )
        added: list[dict[str, Any]] = []
        for sh in shareholders:
            record = {"shareholder_id": new_id("sh"), **sh}
            self.shareholders.append(record)
            added.append(record)
        return {"shareholders": added}


# -- MonetizationPaymentClient: QAR 6,000 onboarding-fee flow (Phase 3) ------


@runtime_checkable
class MonetizationPaymentClient(Protocol):
    """Five-tool monetization fee flow for the QAR 6,000 onboarding payment.

    Implementations: ``InMemoryMonetizationPaymentClient`` (tests) and
    ``McpMonetizationPaymentAdapter`` (production — wraps
    ``madad_kyc_get_business_details`` plus the five ``madad_payments_*``
    monetization tools).

    Per Ishan's 2026-05-31 backend update, ``create_monetization_payment`` and
    ``send_monetization_payment_link`` accept ``idempotency_key`` as a TOOL
    PARAMETER (not an HTTP header) — the backend dedupes on it. The Phase 1
    provider already adds these two tools to the default ``idempotent_tools``
    set, so the MCP client retries them transparently on transient transport
    errors with the same key.
    """

    async def get_business_details(self, *, access_token: str) -> dict[str, Any]: ...

    async def list_monetization_products(
        self, *, access_token: str
    ) -> dict[str, Any]: ...

    async def create_monetization_payment(
        self,
        *,
        access_token: str,
        business_details_id: str,
        product_id: str,
        amount_qar: int,
        idempotency_key: str,
    ) -> dict[str, Any]: ...

    async def send_monetization_payment_link(
        self,
        *,
        access_token: str,
        payment_id: str,
        channel: Channel,
        identity: str,
        idempotency_key: str,
    ) -> dict[str, Any]: ...

    async def get_monetization_payment(
        self, *, access_token: str, payment_id: str
    ) -> dict[str, Any]: ...

    async def sync_monetization_payment_status(
        self, *, access_token: str, payment_id: str
    ) -> dict[str, Any]: ...


class InMemoryMonetizationPaymentClient:
    """Configurable fake implementing :class:`MonetizationPaymentClient`.

    Tests seed ``business_details`` and ``products``; the fake models the
    real backend's idempotency-key dedupe — repeated ``create`` calls with
    the same key return the same ``payment_id`` instead of creating a new
    record. ``sync_status`` marks a payment paid; ``get`` reads the current
    record. Every call is captured in ``calls`` for introspection.
    """

    def __init__(
        self,
        *,
        business_details: dict[str, Any] | None = None,
        products: list[dict[str, Any]] | None = None,
    ) -> None:
        self._business_details: dict[str, Any] = dict(
            business_details or {"business_details_id": "biz-1", "name": "Test SME"}
        )
        self._products: list[dict[str, Any]] = list(
            products
            or [
                {
                    "product_id": "prod-monetization",
                    "name": "Onboarding Fee",
                    "amount_qar": 6000,
                }
            ]
        )
        self.payments: dict[str, dict[str, Any]] = {}
        self._by_idempotency_key: dict[str, str] = {}
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def _record(self, name: str, **kwargs: Any) -> None:
        self.calls.append((name, kwargs))

    async def get_business_details(self, *, access_token: str) -> dict[str, Any]:
        self._record("get_business_details", access_token=access_token)
        return dict(self._business_details)

    async def list_monetization_products(
        self, *, access_token: str
    ) -> dict[str, Any]:
        self._record("list_monetization_products", access_token=access_token)
        return {"products": list(self._products)}

    async def create_monetization_payment(
        self,
        *,
        access_token: str,
        business_details_id: str,
        product_id: str,
        amount_qar: int,
        idempotency_key: str,
    ) -> dict[str, Any]:
        self._record(
            "create_monetization_payment",
            access_token=access_token,
            business_details_id=business_details_id,
            product_id=product_id,
            amount_qar=amount_qar,
            idempotency_key=idempotency_key,
        )
        # Backend-side dedupe: same key → same payment record.
        existing_id = self._by_idempotency_key.get(idempotency_key)
        if existing_id is not None:
            return dict(self.payments[existing_id])
        payment_id = new_id("pay")
        record = {
            "payment_id": payment_id,
            "business_details_id": business_details_id,
            "product_id": product_id,
            "amount_qar": amount_qar,
            "status": "CREATED",
            "idempotency_key": idempotency_key,
            # Mirror the real cluster: paymentLink + providerOrderNumber are
            # returned ON CREATE so callers don't need to call send_link.
            "paymentLink": f"https://pay.madad.example/{payment_id}",
            "providerOrderNumber": f"MADAD-ONBOARDING-{payment_id}",
        }
        self.payments[payment_id] = record
        self._by_idempotency_key[idempotency_key] = payment_id
        return dict(record)

    async def send_monetization_payment_link(
        self,
        *,
        access_token: str,
        payment_id: str,
        channel: Channel,
        identity: str,
        idempotency_key: str,
    ) -> dict[str, Any]:
        self._record(
            "send_monetization_payment_link",
            access_token=access_token,
            payment_id=payment_id,
            channel=channel,
            identity=identity,
            idempotency_key=idempotency_key,
        )
        record = self.payments.get(payment_id, {})
        link = f"https://pay.madad.example/{payment_id}"
        record["payment_link"] = link
        record["link_sent_to"] = identity
        return {"payment_id": payment_id, "payment_link": link, "channel": str(channel)}

    async def get_monetization_payment(
        self, *, access_token: str, payment_id: str
    ) -> dict[str, Any]:
        self._record(
            "get_monetization_payment",
            access_token=access_token,
            payment_id=payment_id,
        )
        return dict(self.payments.get(payment_id, {"payment_id": payment_id}))

    async def sync_monetization_payment_status(
        self, *, access_token: str, payment_id: str
    ) -> dict[str, Any]:
        self._record(
            "sync_monetization_payment_status",
            access_token=access_token,
            payment_id=payment_id,
        )
        record = self.payments.get(payment_id)
        if record is not None:
            record["status"] = "PAID"
        return dict(record or {"payment_id": payment_id, "status": "UNKNOWN"})

    def mark_paid(self, payment_id: str) -> None:
        """Test helper: simulate the backend marking a payment paid (so the
        next get/sync returns PAID without going through the sync tool)."""

        record = self.payments.get(payment_id)
        if record is not None:
            record["status"] = "PAID"


# -- Reminders (via Nudge service) -------------------------------------------


class Reminders(ABC):
    @abstractmethod
    async def schedule(
        self,
        reason: str,
        *,
        channel: Channel,
        identity: str,
        target_ref: str | None,
        variables: dict[str, Any] | None = None,
    ) -> None: ...

    @abstractmethod
    async def suppress(self, *, target_ref: str | None) -> None: ...


class RecordingReminders(Reminders):
    def __init__(self) -> None:
        self.scheduled: list[str] = []
        self.suppressed: list[str | None] = []
        # Full call log: ``(reason, {channel, identity, target_ref, variables})``.
        # Tests inspect ``variables`` to verify A9 (payment-link + missing-docs
        # threading).
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def schedule(
        self,
        reason: str,
        *,
        channel: Channel,
        identity: str,
        target_ref: str | None,
        variables: dict[str, Any] | None = None,
    ) -> None:
        self.scheduled.append(reason)
        self.calls.append((
            reason,
            {
                "channel": channel,
                "identity": identity,
                "target_ref": target_ref,
                "variables": variables or {},
            },
        ))

    async def suppress(self, *, target_ref: str | None) -> None:
        self.suppressed.append(target_ref)


# -- Phase 1.b invoice financing ------------------------------------------
# Tools live in the cluster under ``madad_invoices_*``. Per Ishan's README
# §"Step 10: Invoice Submission" and [[project_mcp_catalog]] §"Invoice
# Financing", the preferred WhatsApp/email agent path for a SINGLE invoice
# is ``extract_and_submit_invoice_base64`` (combines extract+submit in one
# round-trip; backend reuses the portal pipeline). Bulk invoices go via
# ``upload_zip`` (server-side recursive). Status queries call
# ``get_my_invoices``.
class InvoiceClient(Protocol):
    """Phase 1.b invoice financing client — the SME submits invoices over
    WhatsApp/email once the credit line is ACTIVATED, and the backend then
    extracts + submits them through the same pipeline the portal uses.

    Implementations: ``InMemoryInvoiceClient`` (tests) and
    ``McpInvoiceClient`` (production — wraps the 9 ``madad_invoices_*``
    tools from the MCP cluster).
    """

    async def extract_and_submit_base64(
        self,
        *,
        access_token: str,
        filename: str,
        content_base64: str,
        mime_type: str | None = None,
        user_id: str | None = None,
        status: str = "UNVERIFIED",
    ) -> dict[str, Any]:
        """Preferred WhatsApp/email path for a SINGLE invoice attachment.

        One call: backend extracts the invoice fields (supplier, customer,
        amount, due_date, etc.) and submits the record in one round-trip.
        Returns the submitted invoice record so the agent can show the SME
        the extracted summary and a backend-assigned reference number.
        """
        ...

    async def submit_zip_base64(
        self,
        *,
        access_token: str,
        filename: str,
        content_base64: str,
        user_id: str | None = None,
        status: str = "UNVERIFIED",
        continue_on_error: bool = True,
    ) -> dict[str, Any]:
        """Bulk path — the SME sent a ZIP. Backend expands the archive,
        runs extract+submit per member, returns the per-file outcome list
        so the agent can render a ZIP receipt (✅ / ⚠️ per invoice)."""
        ...

    async def get_my_invoices(
        self, *, access_token: str
    ) -> dict[str, Any]:
        """Status query — used when the SME asks 'what's the status of my
        invoices?'. Returns the SME's invoice list with backend-computed
        statuses (SUBMITTED / DISBURSED / REPAID / etc.)."""
        ...


class InMemoryInvoiceClient:
    """Configurable fake implementing :class:`InvoiceClient`.

    Tests can seed ``invoices`` to drive status-query rendering. Submitted
    invoices accumulate in ``submitted`` so workflow assertions can
    inspect what was sent. Each call is captured in ``calls`` for
    introspection. ZIP submissions append one entry per member to
    ``submitted`` so the per-file checklist is faithful to production.
    """

    def __init__(
        self,
        *,
        invoices: list[dict[str, Any]] | None = None,
        extract_error: BaseException | None = None,
    ) -> None:
        self._seeded: list[dict[str, Any]] = list(invoices or [])
        self.submitted: list[dict[str, Any]] = []
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self._extract_error = extract_error

    def _record(self, name: str, **kwargs: Any) -> None:
        self.calls.append((name, kwargs))

    async def extract_and_submit_base64(
        self,
        *,
        access_token: str,
        filename: str,
        content_base64: str,
        mime_type: str | None = None,
        user_id: str | None = None,
        status: str = "UNVERIFIED",
    ) -> dict[str, Any]:
        self._record(
            "extract_and_submit_base64",
            access_token=access_token,
            filename=filename,
            mime_type=mime_type,
            user_id=user_id,
            status=status,
        )
        if self._extract_error is not None:
            raise self._extract_error
        record = {
            "invoice_id": new_id("inv"),
            "filename": filename,
            "status": status,
            "supplier_name": "Test Supplier",
            "customer_name": "Test Customer",
            "total_amount": 1000,
            "currency": "QAR",
            "invoice_number": f"INV-{len(self.submitted) + 1}",
        }
        self.submitted.append(record)
        return record

    async def submit_zip_base64(
        self,
        *,
        access_token: str,
        filename: str,
        content_base64: str,
        user_id: str | None = None,
        status: str = "UNVERIFIED",
        continue_on_error: bool = True,
    ) -> dict[str, Any]:
        self._record(
            "submit_zip_base64",
            access_token=access_token,
            filename=filename,
            user_id=user_id,
            status=status,
            continue_on_error=continue_on_error,
        )
        # Synthesise a 2-invoice ZIP per call so workflow tests don't need
        # to build real ZIP bytes just to exercise the bulk path.
        results = []
        for i in range(1, 3):
            record = {
                "invoice_id": new_id("inv"),
                "filename": f"invoice_{i}.pdf",
                "status": status,
                "supplier_name": "Test Supplier",
                "total_amount": 500 * i,
                "currency": "QAR",
                "confidently_extracted": True,
            }
            self.submitted.append(record)
            results.append(record)
        return {"invoices": results, "total": len(results), "failed": 0}

    async def get_my_invoices(
        self, *, access_token: str
    ) -> dict[str, Any]:
        self._record("get_my_invoices", access_token=access_token)
        # Return seeded + everything we've submitted in this run.
        return {"invoices": list(self._seeded) + list(self.submitted)}
