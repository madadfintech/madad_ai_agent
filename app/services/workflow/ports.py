"""Business ports the onboarding workflow orchestrates through.

The workflow nodes never touch external systems directly — they call these ports.
Real adapters bridge to the Communication / Document / Nudge services (and, via
MCP, to Madad's APIs and Tess); in-memory fakes drive deterministic tests.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections import defaultdict
from typing import Any, Literal, Protocol, runtime_checkable

from pydantic import BaseModel, Field

from app.shared.workflow.enums import Channel
from app.shared.workflow.utils import new_id

SessionType = Literal["existing_user", "new_lead"]
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

    def templates(self) -> list[str]:
        return [s["template_key"] for s in self.sent]


# -- Document intake (via Document Intelligence service) ----------------------


class DocumentIntake(ABC):
    @abstractmethod
    async def ingest(
        self, *, application_ref: str | None, filename: str, provider_ref: str | None = None
    ) -> None: ...

    @abstractmethod
    async def missing(self, *, checklist: str, application_ref: str | None) -> list[str]: ...


class InMemoryDocumentIntake(DocumentIntake):
    """Simulates document processing + checklist tracking for tests.

    Classifies an ingested file by a filename-keyword map and computes the still
    -missing required codes for an application.
    """

    def __init__(self, *, required: list[str], type_by_keyword: dict[str, str]) -> None:
        self._required = required
        self._types = type_by_keyword
        self._received: dict[str, set[str]] = defaultdict(set)

    async def ingest(
        self, *, application_ref: str | None, filename: str, provider_ref: str | None = None
    ) -> None:
        key = application_ref or "_"
        lowered = filename.lower()
        for keyword, doc_type in self._types.items():
            if keyword in lowered:
                self._received[key].add(doc_type)
                break

    async def missing(self, *, checklist: str, application_ref: str | None) -> list[str]:
        received = self._received[application_ref or "_"]
        return [code for code in self._required if code not in received]


# -- Madad backend (via MCP) -------------------------------------------------
#
# DEPRECATED: ``MadadClient`` / ``InMemoryMadadClient`` model the workflow's
# original "RPC decisions" assumption (check_eligibility, request_score, ...).
# Those tools do NOT exist in the real catalog. The Phase 2 graph reshape
# removes the nodes that call these methods; both classes will be deleted
# alongside that change. They remain here only to keep the existing
# tests/onboarding/test_*.py suite green during Phases 1–2.

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
    ) -> dict[str, Any]: ...

    async def me(self, *, access_token: str) -> dict[str, Any]: ...

    async def refresh(self, *, refresh_token: str) -> AuthTokens: ...

    async def logout(self, *, access_token: str) -> None: ...


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
    ) -> ChannelSession:
        self._record(
            "open_session",
            channel=channel,
            identifier=identifier,
            email=email,
            phone=phone,
            display_name=display_name,
            create_onboarding_token=create_onboarding_token,
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
    ) -> dict[str, Any]:
        self._record(
            "complete_onboarding",
            first_name=first_name,
            last_name=last_name,
            onboarding_token=onboarding_token,
            email=email,
            phone_number=phone_number,
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
        self, *, access_token: str, content_base64: str, filename: str
    ) -> dict[str, Any]: ...

    async def update_eligibility(
        self, *, access_token: str, data: dict[str, Any]
    ) -> dict[str, Any]: ...

    async def upload_audited_financial_report(
        self, *, access_token: str, content_base64: str, filename: str
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
    ) -> dict[str, Any]: ...

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
        self, *, access_token: str, content_base64: str, filename: str
    ) -> dict[str, Any]:
        self._record(
            "upload_commercial_registration",
            access_token=access_token,
            filename=filename,
        )
        self.cr_document = {"filename": filename, "content_base64": content_base64}
        return {"document_id": new_id("cr"), "filename": filename}

    async def update_eligibility(
        self, *, access_token: str, data: dict[str, Any]
    ) -> dict[str, Any]:
        self._record("update_eligibility", access_token=access_token, data=data)
        return self._eligibility_result

    async def upload_audited_financial_report(
        self, *, access_token: str, content_base64: str, filename: str
    ) -> dict[str, Any]:
        self._record(
            "upload_audited_financial_report",
            access_token=access_token,
            filename=filename,
        )
        self.financial_report = {"filename": filename, "content_base64": content_base64}
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
    ) -> dict[str, Any]:
        self._record(
            "upload_document_base64",
            access_token=access_token,
            filename=filename,
            document_type=document_type,
        )
        self.uploaded_documents[document_type] = {
            "filename": filename,
            "content_base64": content_base64,
        }
        return {"document_id": new_id("doc"), "document_type": document_type}

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


class MadadClient(ABC):
    """[DEPRECATED] Original RPC-decisions port. Tools listed here don't exist
    in the real catalog — kept only so the pre-Phase-2 onboarding tests build.
    Slated for deletion in Phase 2 (graph reshape)."""

    @abstractmethod
    async def check_eligibility(self, cr_ref: str | None) -> bool: ...

    @abstractmethod
    async def request_prequalification(self, *, identity: str, cr_ref: str | None) -> str: ...

    @abstractmethod
    async def request_score(self, application_ref: str | None) -> None: ...

    @abstractmethod
    async def submit_to_lenders(self, application_ref: str | None) -> None: ...

    @abstractmethod
    async def activate_credit_line(
        self, application_ref: str | None, offer: dict[str, Any]
    ) -> dict[str, Any]: ...


class InMemoryMadadClient(MadadClient):
    """[DEPRECATED] See :class:`MadadClient`."""

    def __init__(self, *, eligible: bool = True) -> None:
        self.eligible = eligible
        self.calls: list[str] = []

    async def check_eligibility(self, cr_ref: str | None) -> bool:
        self.calls.append("check_eligibility")
        return self.eligible

    async def request_prequalification(self, *, identity: str, cr_ref: str | None) -> str:
        self.calls.append("request_prequalification")
        return new_id("madad")

    async def request_score(self, application_ref: str | None) -> None:
        self.calls.append("request_score")

    async def submit_to_lenders(self, application_ref: str | None) -> None:
        self.calls.append("submit_to_lenders")

    async def activate_credit_line(
        self, application_ref: str | None, offer: dict[str, Any]
    ) -> dict[str, Any]:
        self.calls.append("activate_credit_line")
        return {"active": True, **offer}


# -- Payments (Tess, via MCP) ------------------------------------------------


class PaymentClient(ABC):
    @abstractmethod
    async def create_link(self, *, application_ref: str | None, amount: int) -> str: ...


class InMemoryPaymentClient(PaymentClient):
    def __init__(self) -> None:
        self.links: list[str] = []

    async def create_link(self, *, application_ref: str | None, amount: int) -> str:
        link = f"https://pay.tess.example/{application_ref or 'app'}/{amount}"
        self.links.append(link)
        return link


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

    async def suppress(self, *, target_ref: str | None) -> None:
        self.suppressed.append(target_ref)
