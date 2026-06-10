"""Onboarding workflow state and reply helpers."""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Any, Literal

from pydantic import Field

from app.shared.workflow.state import WorkflowState

from .ports import ChannelSession, ContactCheckResult, SessionType

StatusSource = Literal["webhook", "poll", "chat"]


class JourneyStatus(StrEnum):
    """Canonical Madad onboarding journey statuses.

    Values are the exact strings returned by ``madad_auth_me`` in
    ``user.journeyStatus``. Authoritative meanings live in Ishan's MCP cluster
    README (status reference table, 2026-06-01). The Phase 2 router branches on
    these — see [[project_mcp_catalog]] § 'CANONICAL JOURNEY STATUS REFERENCE'.
    """

    SIGN_UP = "SIGN_UP"
    ONBOARDED = "ONBOARDED"
    IN_ELIGIBLE = "IN_ELIGIBLE"
    ELIGIBLE = "ELIGIBLE"
    INCOMPLETE = "INCOMPLETE"
    UNVERIFIED = "UNVERIFIED"
    VERIFIED = "VERIFIED"
    PRE_QUALIFIED = "PRE_QUALIFIED"
    QUALIFIED = "QUALIFIED"
    UNQUALIFIED = "UNQUALIFIED"
    ACCEPTED = "ACCEPTED"
    NOT_ACCEPTED = "NOT_ACCEPTED"
    OFFER_ACCEPTED = "OFFER_ACCEPTED"
    OFFER_EXPIRED = "OFFER_EXPIRED"
    OPEN = "OPEN"
    ACTIVATED = "ACTIVATED"


# Terminal status groups — used by the polling worker (Phase 4) to skip runs
# that won't advance further from the agent's side.
TERMINAL_FAIL_STATUSES: frozenset[JourneyStatus] = frozenset(
    {JourneyStatus.IN_ELIGIBLE, JourneyStatus.UNQUALIFIED, JourneyStatus.NOT_ACCEPTED}
)
TERMINAL_SUCCESS_STATUSES: frozenset[JourneyStatus] = frozenset(
    {JourneyStatus.ACTIVATED}
)


class OnboardingState(WorkflowState):
    """Typed state for the Phase 1.a onboarding workflow (Steps 1–8)."""

    locale: str = "en"

    # -- Identity (channel session bridge + auth) ----------------------------
    # The verified channel identity we resolved with the MCP bridge (e.g. the
    # WhatsApp E.164 or email address). May equal WorkflowState.identity, but
    # kept separate so workflow-state tooling and identity-resolution state
    # can evolve independently.
    channel_identity: str | None = None
    # Full ChannelSession returned by ``madad_mcp_create_channel_session``.
    channel_session_response: ChannelSession | None = None
    # Convenience fields the workflow router branches on; populated alongside
    # ``channel_session_response``.
    session_type: SessionType | None = None
    # QA #5 security (2026-06-09): tokens are short-lived and SHOULD NOT
    # land in LangGraph checkpoints. The fields stay (so the workflow
    # can still pass them between nodes within one execution) but are
    # marked exclude=True so ``model_dump()`` — which the checkpoint
    # serializer goes through — strips them before persistence. On
    # resume, _live_token mints a fresh one from the verified channel
    # identity (no password, no login) so behaviour is unchanged.
    access_token: str | None = Field(default=None, exclude=True)
    onboarding_token: str | None = Field(default=None, exclude=True)
    refresh_token: str | None = Field(default=None, exclude=True)
    token_expires_at: int | None = Field(default=None, exclude=True)  # unix epoch seconds
    madad_user_id: str | None = None
    # Q8 three-way result on first contact (for the check_contact router).
    check_contact_result: ContactCheckResult | None = None
    # Per Ishan (cluster e6ea5d2, 2026-06-10): the read-only registration
    # lookup runs alongside check_contact and returns the full registered-
    # user shape (route hint, journey status, fee paid flag, credit line,
    # offers...). The dispatcher uses ``registration_route`` to skip the
    # SIGN_UP path for returning users (Bug #2 + #6). Raw payload stored
    # so any field needed downstream — referenceNumber, offers list,
    # credit line details — is accessible without a second call.
    registration_route: str | None = None
    registration_payload: dict[str, Any] = Field(default_factory=dict)
    # When the third branch fires; the domain the email belongs to.
    domain_block_reason: str | None = None

    # -- Journey (the canonical status the agent routes off) -----------------
    journey_status: JourneyStatus | None = None
    last_polled_at: datetime | None = None
    last_status_source: StatusSource | None = None
    # Step 1: new-lead onboarding-details capture (precedes complete_onboarding).
    # ALL 9 fields the cluster's AUTH_COMPLETE_ONBOARDING tool requires for a
    # fresh signup; email/phone are normally derived from the channel identity
    # but can be overridden via the intake form.
    onboarding_first_name: str | None = None
    onboarding_last_name: str | None = None
    onboarding_legal_entity_name: str | None = None
    onboarding_cr_number: str | None = None
    onboarding_is_qatar_based: bool | None = None
    onboarding_role: str | None = None
    onboarding_email_override: str | None = None
    onboarding_phone_override: str | None = None

    # Step 1–2: campaign + consent + CR.
    entry_reply: str = ""
    # Business-email step (right after YES, before consent/CR). business_email
    # holds the captured address; business_email_status drives the router:
    # "ok" -> proceed to consent/CR, "conflict" -> ask for a different email,
    # "portal" -> existing portal account (log in), None/"" -> still awaiting.
    business_email: str | None = None
    business_email_status: str | None = None
    consent: bool = False
    cr_ref: str | None = None
    cr_filename: str | None = None
    cr_content_base64: str | None = None
    cr_mime_type: str | None = None

    # Step 3–4: eligibility + financials.
    eligibility_form_data: dict[str, Any] = Field(default_factory=dict)
    eligible: bool | None = None
    financials_received: bool = False
    financials_filename: str | None = None
    financials_content_base64: str | None = None
    financials_mime_type: str | None = None
    application_ref: str | None = None  # Madad account ref (e.g. #7388266)

    # Step 5–6: dynamic checklist + counterparties.
    missing_documents: list[str] = []
    documents_received: bool = False
    buyers: list[dict[str, Any]] = []
    shareholders: list[dict[str, Any]] = []
    # Per Ishan (UAT 2026-06-10): classifier hangs (notably AoA) cause
    # individual classify calls to fail, leaving required codes "still
    # needed" forever even after the SME has uploaded enough docs. Match
    # the count-based unblock in ``DocumentIntelligenceService`` (PR #4,
    # commit 6c05b1c) — total attachments the SME has actually sent in
    # the post-prequal docs phase, regardless of classification outcome.
    # When this hits ``len(DEFAULT_WHATSAPP_REQUIRED_DOCS)`` the docs
    # loop unblocks even if some required slots are still pending.
    docs_uploaded_count: int = 0
    # Tracks the SME's reply to the "any more documents to upload?" prompt
    # that fires once the docs phase has produced enough uploads. NO → the
    # run advances to payment_wait. YES → the run loops back to the docs
    # upload-await node so they can keep sending.
    more_docs_decision: str | None = None  # "yes" | "no" | None (not asked yet)
    # Bug #11 (UAT 2026-06-09): debounce the ``documents.processing`` ack
    # so the bridge's per-file POST burst (one inbound per ZIP-member,
    # 8+ messages in a few seconds) doesn't spam the user with 8 copies
    # of "📦 Got it — processing your documents now…". Stores the ISO
    # timestamp of the last processing ack; the docs node only re-fires
    # the ack when the previous one is older than DOCS_PROCESSING_ACK_TTL.
    documents_processing_ack_at: str | None = None
    # Bug #15 (UAT 2026-06-09): same shape as the processing-ack debounce
    # but for the full "📋 Application checklist" body. Per-upload still
    # sends a brief ✅ receipt; the full ✅/⚠️ checklist + footer only
    # re-fires after the previous one is older than
    # DOCS_CHECKLIST_TTL_SECONDS so the SME isn't shown a 15-line wall of
    # text for every single file the bridge fans out as a separate POST.
    documents_checklist_sent_at: str | None = None

    # Postman-triggered gates (demo): the pre-qualification result (after the
    # audited report) and the payment step (after the coffee message) are each
    # released by an external trigger rather than auto-advancing.
    prequalified: bool = False
    payment_ready: bool = False
    madad_score: int | None = None
    # Per Ishan (2026-06-07): backend tracks the conversational onboarding
    # step for WhatsApp leads via ``madad_mcp_update_onboarding_progress``.
    # Hard-gates the pre-qualified document checklist on ``step >= 3``.
    # Recorded so the workflow doesn't re-call the tool with a stale (lower)
    # step number on retries / resumes.
    onboarding_progress_step: int | None = None

    # Step 7–8: payment + offers.
    paid: bool = False
    offers: list[dict[str, Any]] = []
    selected_offer: dict[str, Any] | None = None

    # -- Step 5: monetization payment (Phase 3 will populate) ----------------
    business_details_id: str | None = None
    payment_product_id: str | None = None
    payment_id: str | None = None
    payment_link: str | None = None
    payment_provider_ref: str | None = None  # Tess providerOrderNumber
    payment_status: str | None = None  # backend payment record status

    # -- Idempotency keys (deterministic, per-run; backend dedupes payments) -
    # Key by canonical tool name; value = key string passed to the tool. Phase
    # 3 populates this for monetization writes; Phase 1.b extends to invoice
    # operations.
    idempotency_keys: dict[str, str] = Field(default_factory=dict)

    # Terminal/decline tracking.
    outcome: str | None = None  # completed | declined | not_eligible | domain_blocked | ...


def reply_text(value: Any) -> str:
    if isinstance(value, dict):
        return str(value.get("text") or "").strip()
    return str(value or "").strip()


def reply_attachments(value: Any) -> list[dict[str, Any]]:
    if isinstance(value, dict):
        return list(value.get("attachments") or [])
    return []


_YES_TOKENS = frozenset(
    {
        # English
        "YES", "Y", "YEAH", "YEP", "YUP", "YA", "YEAHH",
        "YESS", "YESSS", "YES!", "YES PLEASE", "YES PLS",
        "OK", "OKAY", "OKEY", "K", "KK", "KAY",
        "SURE", "SURELY", "ABSOLUTELY", "DEFINITELY", "OFCOURSE",
        "OF COURSE", "FINE", "ALRIGHT", "ALRITE", "AGREED", "AGREE",
        "DO IT", "GO AHEAD", "GO ON", "LET'S GO", "LETS GO",
        "PROCEED", "CONTINUE", "I AGREE", "SOUNDS GOOD", "GOOD",
        "👍", "✅", "👌",
        # Arabic — kept from the original set
        "نعم", "اوكي", "حسنا",
    }
)

_NO_TOKENS = frozenset(
    {
        "NO", "N", "NOPE", "NOPES", "NAH", "NA",
        "NOT NOW", "NOT INTERESTED", "NEVER MIND", "NEVERMIND",
        "DON'T", "DONT", "STOP", "CANCEL", "DECLINE", "REJECT",
        "DISAGREE", "I DISAGREE", "👎", "❌",
        "لا",
    }
)


def is_yes(value: Any) -> bool:
    return reply_text(value).upper() in _YES_TOKENS


def is_no(value: Any) -> bool:
    return reply_text(value).upper() in _NO_TOKENS
