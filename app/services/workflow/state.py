"""Onboarding workflow state and reply helpers."""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Any, Literal

from pydantic import Field

from app.shared.workflow.state import WorkflowState

from .ports import ChannelSession, ContactCheckResult, SessionType

StatusSource = Literal["webhook", "poll"]


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
    access_token: str | None = None
    onboarding_token: str | None = None
    refresh_token: str | None = None
    token_expires_at: int | None = None  # unix epoch seconds
    madad_user_id: str | None = None
    # Q8 three-way result on first contact (for the check_contact router).
    check_contact_result: ContactCheckResult | None = None
    # When the third branch fires; the domain the email belongs to.
    domain_block_reason: str | None = None

    # -- Journey (the canonical status the agent routes off) -----------------
    journey_status: JourneyStatus | None = None
    last_polled_at: datetime | None = None
    last_status_source: StatusSource | None = None

    # Step 1: new-lead onboarding-details capture (precedes complete_onboarding).
    onboarding_first_name: str | None = None
    onboarding_last_name: str | None = None

    # Step 1–2: campaign + consent + CR.
    entry_reply: str = ""
    consent: bool = False
    cr_ref: str | None = None
    cr_content_base64: str | None = None

    # Step 3–4: eligibility + financials.
    eligibility_form_data: dict[str, Any] = Field(default_factory=dict)
    eligible: bool | None = None
    financials_received: bool = False
    financials_filename: str | None = None
    financials_content_base64: str | None = None
    application_ref: str | None = None  # Madad account ref (e.g. #7388266)

    # Step 5–6: dynamic checklist + counterparties.
    missing_documents: list[str] = []
    buyers: list[dict[str, Any]] = []
    shareholders: list[dict[str, Any]] = []

    # Step 7–8: payment + offers.
    paid: bool = False
    offers: list[dict[str, Any]] = []
    selected_offer: dict[str, Any] | None = None

    # -- Step 5: monetization payment (Phase 3 will populate) ----------------
    business_details_id: str | None = None
    payment_product_id: str | None = None
    payment_id: str | None = None
    payment_link: str | None = None
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


def is_yes(value: Any) -> bool:
    return reply_text(value).upper() in {"YES", "Y", "نعم"}
