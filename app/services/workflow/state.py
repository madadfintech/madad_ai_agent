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
    # Returning-user resume (set by _resume_status_fetch): whether the existing
    # account already has an email on file. Drives the SIGN_UP/ONBOARDED resume
    # split — no email → ask for it first; has email → straight to consent/CR.
    account_has_email: bool | None = None
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
    # Whether the document uploaded at the CR step CONFIRMED as a Commercial
    # Registration. Gates the "registered in Qatar — all good" affirmation:
    # defaults FALSE and is set True ONLY when the classifier confidently returns
    # commercial_registration — so a logo / any other doc / classifier
    # timeout/uncertainty never produces a false Qatar claim (user 2026-06-14:
    # uploading a logo still triggered the line under the old default-True).
    cr_verified: bool = False
    # CR reupload gate (prod 2026-07-02): when the classifier RUNS and confidently
    # returns a non-CR type (e.g. a random screenshot classified as
    # COMMERCIAL_CREDIT_REPORT), we nudge the SME to re-upload a proper CR instead
    # of silently accepting it. cr_reject_count caps the nudges so a genuine CR the
    # classifier simply can't read is NEVER trapped — after the cap we fail open and
    # proceed (backend extraction + admin review remain the real gate).
    cr_reject_count: int = 0
    cr_needs_reupload: bool = False

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
    # M1 acceptance gap fix (2026-06-28): the CMS-resolved required-docs
    # list, cached here once at session start (``_documents_list_fetch``).
    # All downstream progress meters (``X of N received``, pending-docs
    # summary, LLM off-script context) read from THIS list rather than the
    # Python ``DEFAULT_WHATSAPP_REQUIRED_DOCS`` constant, so when MADAD ops
    # adds a doc via CMS the progress meter immediately reflects "of N+1".
    # Empty when the docs phase hasn't started yet (e.g. pre-eligibility).
    required_documents: list[str] = []
    # The coffee / "all documents received" message must fire exactly ONCE.
    # Set True after _documents_complete sends it; _route_documents then
    # re-parks silently in the upload-await node instead of re-sending it
    # (user 2026-06-12 — kills the repeated "any more documents?" loop).
    documents_complete_sent: bool = False
    # The SME replied NO / "done" to the "any more documents?" prompt while
    # some required docs were still undetected — proceed to the next step
    # anyway (frustrated-user escape hatch, user 2026-06-12).
    docs_proceed: bool = False
    # Debounce for the "any more documents?" prompt: timestamp (ISO) of the
    # last time it was sent. A 20-doc / ZIP burst arrives as 20 separate
    # inbounds — the prompt fires once per burst (within the TTL), never per
    # doc. Reset implicitly by the TTL window.
    more_docs_prompt_at: str | None = None
    # ISO timestamp of the most recent upload wave. Used to detect a NEW upload
    # session (a gap since the last wave) so the checklist + "any more?" prompt
    # fires once per session — not per wave (UAT 2026-06-13).
    docs_last_upload_at: str | None = None
    # Doc types we've already sent a receipt for (✅ validated or ⏳ received), so
    # a multi-wave bulk upload never re-acknowledges the same doc — which
    # previously produced contradictory "✅ X validated" then "⏳ X received"
    # receipts and repeated ⏳ for the same file (UAT 2026-06-13).
    docs_acked: list[str] = []
    # Set once the end-of-upload settle prompt (checklist + tappable YES/NO
    # buttons) has been sent for the current quiet period; reset on each new
    # upload wave so a later batch re-arms it (UAT 2026-06-13).
    docs_settle_prompted: bool = False
    # Signature of the missing-doc set the LAST time we actually sent the "any
    # more?" prompt (sorted doc keys joined). This is the structural
    # duplicate-guard (user 2026-06-21: "we absolutely CANNOT have multiple
    # messages"): the prompt only fires when the pending set DIFFERS from this
    # — so repeated settle resumes for the same pending set are inert, while a
    # genuine new upload (which shrinks the set) re-arms it exactly once. Time
    # windows can race; a content signature cannot.
    more_docs_prompt_sig: str | None = None
    # Deprecated (2026-06-12): replaced by the in-loop is_no/is_yes handling.
    # Retained for checkpoint back-compat; no longer read.
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
    # UAT 2026-06-17 gap fix: when the admin marks the SME as not
    # pre-qualified the dispatcher sets this flag on the resume payload
    # and ``_route_prequalify_wait`` jumps to the not-pre-qualified
    # terminal (silent dead-end otherwise).
    prequalification_rejected: bool = False
    # Set True when the CR processing reports the business is registered
    # outside Qatar. ``cr_upload_base64`` reads is_qatar_based from the
    # backend response; the next routing tick fires the not-Qatar terminal.
    not_qatar_based: bool = False
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
    # UAT 2026-06-17 (+919497191690 screenshot): the status poller can
    # re-enter ``_payment_await`` with paid=True after the run advanced,
    # firing ``onboarding.payment.confirmed`` a second time. One-shot
    # flag so the SME never sees two "Payment received" messages.
    payment_confirmed_sent: bool = False
    offers: list[dict[str, Any]] = []
    selected_offer: dict[str, Any] | None = None
    # One-time guard for the ✅ "offer selected" confirmation so a later
    # background poll that still reports OFFER_ACCEPTED can't re-send it.
    offer_confirmed_sent: bool = False
    # Signature of the offer set last shown to the SME. While parked waiting for
    # a portal selection the status poller re-enters the ACCEPTED→offers route
    # every ~minute; this lets us re-send the offer cards ONLY when a new lender
    # actually adds an offer, not on every routine poll.
    offers_shown_sig: str | None = None
    # UAT 2026-06-17: parallel sig but for the preview-cards node. Without
    # this guard ``_offer_view_send`` re-sent the cards 40s apart when the
    # poller re-entered the route before ``_offer_handoff_to_madad`` (the
    # only writer of ``offers_shown_sig``) had recorded the sig. We can't
    # share the sig field because that would suppress the handoff node's
    # one-shot send.
    offers_preview_shown_sig: str | None = None
    # UAT 2026-06-19 QA #6b: backend emits one ``offers.available`` event
    # per lender, so a 2-lender application produces two events seconds
    # apart. Without a debounce the SME got the "offers ready" message
    # twice — first with 1 bank, then with 2. ``_offer_view_send`` now
    # stamps this on the FIRST arrival and re-parks silently; the next
    # status-poll re-entry (60s cadence) sees the elapsed window and
    # sends ONE consolidated message with the full offer set. The
    # 30s window covers the observed inter-lender gap without keeping
    # the SME waiting more than the existing poll cadence.
    offers_first_seen_at: str | None = None

    # -- Step 5: monetization payment (Phase 3 will populate) ----------------
    business_details_id: str | None = None
    payment_product_id: str | None = None
    # Live amount in QAR sourced from the monetization product (UAT 2026-06-14)
    # so the SME-facing fee tracks whatever Madad ops priced the product at
    # — no more hardcoded "QAR 6,000" when ops bumps the fee.
    payment_amount_qar: int | None = None
    payment_id: str | None = None
    payment_link: str | None = None
    payment_provider_ref: str | None = None  # Tess providerOrderNumber
    payment_status: str | None = None  # backend payment record status

    # -- Idempotency keys (deterministic, per-run; backend dedupes payments) -
    # Key by canonical tool name; value = key string passed to the tool. Phase
    # 3 populates this for monetization writes; Phase 1.b extends to invoice
    # operations.
    idempotency_keys: dict[str, str] = Field(default_factory=dict)

    # -- Phase 1.b: invoice financing ledger --------------------------------
    # Each accepted invoice is appended once (invoice_id, supplier_name,
    # total_amount, currency, status, submitted_at). Lets the agent answer
    # "what's the status of my invoices?" without a backend round-trip when
    # the SME has just submitted one, and gives the disbursement webhook a
    # local record to reconcile against. Disbursements + repayments accrete
    # similarly so the SME can see a running history over WhatsApp.
    invoices_submitted: list[dict[str, Any]] = Field(default_factory=list)
    disbursements_received: list[dict[str, Any]] = Field(default_factory=list)
    repayments_recorded: list[dict[str, Any]] = Field(default_factory=list)
    # Running outstanding balance the SME sees on repayment messages. The
    # backend is the source of truth — this is the agent's optimistic mirror
    # so the message can render a number without an extra read.
    repayment_outstanding_qar: int | None = None

    # UAT 2026-06-17 (+919497191690 screenshot 2:58/2:59 PM): SME saw
    # the "Got your invoice — reading it now…" ack TWICE 64s apart for
    # ONE upload. Same root cause as the duplicate failure 53s after —
    # Meta/WhatsApp delivered the same attachment twice (or the bridge
    # fanned it out). Fingerprint the most recent invoice attempt so a
    # second arrival of the SAME content within the dedupe window is
    # silently dropped (no ack, no extract call, no failure message).
    last_invoice_attempt_sig: str | None = None
    last_invoice_attempt_at: str | None = None
    # UAT 2026-06-19 QA #2: even with the 5min retry window, the agent
    # was submitting each invoice ~3× — status_poll / event resumes
    # were re-entering ``_invoice_collect_await`` with the same
    # attachment payload from a stale state, and the time-window dedupe
    # missed re-entries OUTSIDE the window. PERMANENT submitted-sig
    # tracker: every sig that was actually submitted lands here for
    # the run's lifetime; the invoice node refuses to re-submit a
    # sig it has already processed. Bounded to the most recent 200
    # to keep state small (a single SME never submits 200 invoices
    # in one onboarding run).
    invoice_submitted_sigs: list[str] = []

    # UAT 2026-06-16 (#3): single-invoice confirm-card flow staging.
    # When the SME sends a PDF, we extract (no submit) → render the
    # confirm card → park here until they tap Approve / Edit / Reject.
    # On Approve we re-submit with the (possibly edited) fields, so the
    # base64 content has to be carried across the confirm await. Stripped
    # to None on Approve / Reject so it doesn't linger in checkpoints.
    pending_invoice_draft: dict[str, Any] | None = None
    pending_invoice_filename: str | None = None
    # NOT exposed via /ops/runs/{id} — PII + non-trivial size.
    pending_invoice_content_b64: str | None = Field(default=None, exclude=True)
    pending_invoice_mime: str | None = None
    # When the SME tapped "Edit", the field name they want to change.
    pending_invoice_edit_field: str | None = None

    # UAT 2026-06-16 (#4): bulk ZIP CSV-preview flow staging. Each entry
    # is ``{row, draft, content_b64 (excluded), filename, mime, flag}``.
    # APPROVE ALL submits every row; EDIT/REMOVE mutate the batch in
    # place; on submit the batch clears. ``content_b64`` is stripped
    # from /ops/runs/{id} state and from checkpoint snapshots via
    # exclude=True on the parent list — we mark it via the per-row
    # name and the snapshot-scrubber's allow-list.
    pending_invoice_batch: list[dict[str, Any]] = Field(
        default_factory=list, exclude=True,
    )
    pending_invoice_batch_total_qar: int | None = None
    pending_invoice_batch_currency: str | None = None

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
