"""Phase 2 onboarding workflow — MCP-backed onboarding graph.

Implements the Madad onboarding flow against the real MCP tool catalog
(``madad_auth_*`` + ``madad_kyc_*``). Replaces the Phase 1.a stub graph that
called fabricated RPC ports (``check_eligibility`` / ``request_score`` /
``submit_to_lenders`` / ``activate_credit_line``); those ports — ``MadadClient``
and friends — are deleted in this commit.

The graph follows Ishan's "Final Agent Flow Contract" (MCP cluster README):

  Step 1 (entry + identification):
    campaign_send/await
      → route_entry (YES → check_contact, NO → declined)
    check_contact_send/await
      → route_check_contact (existing | new | blocked)
        - existing  → channel_session_first (one bridge call → access_token)
        - new       → collect_onboarding_details_send/await
                      → complete_onboarding_send
                      → channel_session_second (second bridge call to
                        re-mint the access_token for the now-promoted user)
        - blocked   → domain_blocked terminal

  Step 2 (consent + CR upload):  consent_send/await → cr_upload_base64

  Step 3 (eligibility intake):
    eligibility_intake_send/await → eligibility_update
      → route_eligibility_status (eligible | ineligible)

  Step 4 (audited financials): financials_send/await → financials_upload_base64

  Step 5–6 (admin-requested docs + counterparties):
    documents_list_fetch
      → buyers_collect_send/await
      → shareholders_collect_send/await
      → documents_upload_loop_send/await
      → route_documents (complete | missing — missing loops back)
      → documents_complete

  Step 7 (status poll + payment):
    status_poll_on_demand → route_journey_status (16-status canonical branch)
      payment     → payment_send/await → route_payment (paid → lender_status_poll;
                    unpaid self-loops the await — Phase 3 wires the real
                    payment chain with idempotency keys)
      ineligible  → not_eligible terminal
      unqualified → not_qualified terminal (UNQUALIFIED, NOT_ACCEPTED)
      offers      → offers_fetch
      activated   → activated terminal (terminal-success)
      wait        → journey_wait_await → status_poll_on_demand

  Step 8 (lender + offers + handoff):
    lender_status_poll → route_journey_status
      offers      → offers_fetch
      unqualified → not_qualified
      activated   → activated
      wait        → lender_wait_await → lender_status_poll
    offers_fetch → offer_view_send → offer_handoff_to_madad terminal

The double-session-call pattern (Step 1, new-lead branch) is per Ishan's
contract: the bridge re-establishes the session after ``complete_onboarding``
promotes the lead so KYC calls have an ``access_token`` instead of an
``onboarding_token``.

Phase 4 wires real webhook receivers to drive the status_update resume
sources; in Phase 2 tests inject status updates via ``runtime.resume(...)``
between turns.
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import io
import os
import re
import zipfile
from datetime import datetime
from typing import Any

import httpx

from app.shared.workflow import (
    GraphBuilder,
    WorkflowContext,
    WorkflowDefinition,
    await_input,
)
from app.shared.workflow.enums import Channel
from app.shared.workflow.state import HistoryEntry

from .mcp_kyc import workflow_doc_type as _workflow_doc_type
from .ports import (
    KycClient,
    MadadIdentityClient,
    Messenger,
    MonetizationPaymentClient,
    Reminders,
)
from .state import JourneyStatus, OnboardingState, is_no, is_yes, reply_attachments, reply_text

# QAR 6,000 is the current monetization onboarding fee (Madad ops M-5 may
# vary it by segment later — the workflow falls back to whatever the
# products tool reports; this is the safety default if no products land).
ONBOARDING_FEE_QAR = 6000

TEMPLATE_KEYS = [
    "onboarding.campaign.intro",
    "onboarding.campaign.awaiting_yes_no",
    "onboarding.help.what_is_madad",
    "onboarding.help.security",
    "onboarding.help.contextual",
    "onboarding.declined",
    "onboarding.domain_blocked",
    "onboarding.business_email.ask",
    "onboarding.business_email.conflict",
    "onboarding.collect_details.request",
    "onboarding.consent.request",
    "onboarding.eligibility.intake.request",
    "onboarding.not_eligible",
    "onboarding.financials.request",
    "onboarding.account.created",
    "onboarding.buyers.request",
    "onboarding.shareholders.request",
    "onboarding.documents.checklist",
    "onboarding.documents.missing",
    "onboarding.documents.zip_received",
    "onboarding.documents.single_received",
    "onboarding.documents.complete",
    "onboarding.upload.required",
    "onboarding.cr.received",
    "onboarding.documents.processing",
    "onboarding.documents.more_docs_prompt",
    "onboarding.documents.settle_prompt",
    "onboarding.documents.upload_failed",
    "onboarding.status.pending",
    "onboarding.payment.awaiting",
    "onboarding.payment.confirmed",
    "onboarding.not_qualified",
    "onboarding.payment.request",
    "onboarding.payment.request.button",
    "onboarding.offers.preview",
    "onboarding.offer.handoff",
    "onboarding.offer.handoff.button",
    "onboarding.offer.confirmed",
    "onboarding.activated",
]

# Default values for the seven KYC_UPDATE_ELIGIBILITY fields when the
# operator-supplied form data doesn't include them. Chosen so the staging
# demo against a known-eligible test account submits a passing record
# without an interactive form. Override per-run via the inbound `data`
# payload on the eligibility-intake await.
DEFAULT_ELIGIBILITY_FORM: dict[str, Any] = {
    # All seven values are the actual backend ENUMS the cluster persists,
    # NOT the free-form ints/strings we used to send. Sending the enum
    # directly avoids the silent backend re-mapping we observed in the
    # first verification (e.g. business_age="5" → "UNDER_2_YEARS").
    "is_qatar_based": True,
    "business_age": "UNDER_2_YEARS",     # | OVER_2_YEARS_UNDER_5 | OVER_5_YEARS
    "cr_validity": "UNDER_1_MONTH",      # | OVER_3_MONTHS
    "company_type": "LLC",
    "sector": "trade",
    # turnover / employees stay as strings (cluster's pydantic validator
    # rejects ints) but are free-form numeric strings.
    "turnover": "1000000",
    "employees": "10",
}

HELP_KEYWORDS = (
    "what is madad",
    "who is madad",
    "what's madad",
    "about madad",
)

SECURITY_KEYWORDS = (
    "scam",
    "scammed",
    "fraud",
    "fake",
    "legit",
    "safe",
)

STATUS_KEYWORDS = (
    "status",
    "update",
    "where",
    "progress",
    "application",
)

PORTAL_KEYWORDS = (
    "portal",
    "link",
    "url",
    "madad id",
    "application id",
    "reference",
    # Bug #7+#8 (2026-06-09): natural portal queries the QA testers
    # actually wrote ("how do I login to madadfintech?") were falling
    # through to the generic fallback because none of the original
    # keywords matched. Widen the net.
    "login",
    "log in",
    "logging in",
    "sign in",
    "signin",
    "website",
    "madadfintech",
    "madad fintech",
    "madad.com",
    "madadfintech.com",
)

CASUAL_KEYWORDS = (
    "hi",
    "hello",
    "hey",
    "thanks",
    "thank you",
    "um",
)


def _off_script_template(value: Any) -> str | None:
    text = reply_text(value).lower()
    if any(keyword in text for keyword in SECURITY_KEYWORDS):
        return "onboarding.help.security"
    if any(keyword in text for keyword in HELP_KEYWORDS):
        return "onboarding.help.what_is_madad"
    return None


def _is_status_query(value: Any) -> bool:
    text = reply_text(value).lower()
    return any(keyword in text for keyword in STATUS_KEYWORDS)


def _is_portal_query(value: Any) -> bool:
    text = reply_text(value).lower()
    return any(keyword in text for keyword in PORTAL_KEYWORDS)


def _is_casual_message(value: Any) -> bool:
    text = reply_text(value).lower().strip()
    return any(text == keyword or text.startswith(f"{keyword} ") for keyword in CASUAL_KEYWORDS)


# Bug #16 (UAT 2026-06-09): self-service "what am I still missing?" intent
# matches the spec page 8 "PENDING DOCS" sample queries plus the
# colloquial variants the SME actually types in WhatsApp. Whole-word /
# phrase matching so a stray "list" inside a longer sentence doesn't
# misfire — full text equality OR a clear phrase.
_PENDING_DOCS_PHRASES = (
    "what am i still missing",
    "what's still missing",
    "whats still missing",
    "what is still missing",
    "what's missing",
    "whats missing",
    "what is missing",
    "what's left",
    "whats left",
    "what is left",
    "what's still needed",
    "whats still needed",
    "what's needed",
    "whats needed",
    "what do i still need",
    "what do i need",
    "still needed",
    "still need",
    "pending documents",
    "pending docs",
    "remaining documents",
    "remaining docs",
    "documents checklist",
    "docs checklist",
)
_PENDING_DOCS_SHORT_TOKENS = frozenset(
    {
        "list",
        "checklist",
        "status",
        "pending",
        "missing",
        "remaining",
        "left",
    }
)


def _is_pending_docs_query(value: Any) -> bool:
    """True when the SME is asking for the running pending-docs list."""

    text = reply_text(value).strip().lower()
    if not text:
        return False
    if text in _PENDING_DOCS_SHORT_TOKENS:
        return True
    return any(phrase in text for phrase in _PENDING_DOCS_PHRASES)


def _is_short_negative(value: Any) -> bool:
    text = reply_text(value).lower().strip()
    return is_no(value) or text in {"nope", "not now", "skip", "later"}


def _is_docs_settle(value: Any) -> bool:
    """True for the synthetic ``{"type": "docs_settle"}`` event the status-poller
    sweep delivers once an upload burst has gone quiet — the cue to send the
    end-of-batch checklist + tappable YES/NO button prompt."""
    return isinstance(value, dict) and value.get("type") == "docs_settle"


def _is_inert_system_resume(value: Any) -> bool:
    """True for workflow-internal resume events that carry NO SME-typed text —
    the status-poller's ``status_update`` tick and the docs-settle sweep.

    These are background heartbeats, not chat. A parked wait node that owns a
    canned off-script reply (``payment_wait_await`` / ``prequalify_wait_await``)
    must re-park SILENTLY on them rather than answer every poll cycle: UAT
    2026-06-13 saw ``payment_wait_await`` reply "You're all set…" once a minute
    because the poll tick fell through its trigger check into the off-script
    path. Real backend webhooks carry a ``journey_status`` / ``event`` payload
    and are consumed by each node's trigger checks BEFORE this guard is reached,
    so an actionable resume is never swallowed here."""

    if not isinstance(value, dict):
        return False
    if value.get("type") not in {"status_update", "docs_settle"}:
        return False
    # An SME message riding a ``status_update`` envelope (text present) is real
    # chat — let it reach the contextual responder. Only a bare tick is inert.
    return not reply_text(value).strip()


# -- Smart (LLM) off-script replies ---------------------------------------
# When the SME asks something off-script ("why do you need my CR?", "is this
# safe?", "I already sent everything") we answer it in context with a small
# OpenAI model instead of robotically re-prompting. Falls back to a canned line
# whenever the key is unset or the call fails, so the flow never breaks.

_SMART_SYSTEM_PROMPT = (
    "You are Madad's friendly, professional WhatsApp onboarding assistant, "
    "chatting with a Qatar business owner during their application.\n\n"
    "ABOUT MADAD: Madad Financial Technologies LLC is a Qatar fintech and a "
    "registered participant in the Qatar Central Bank (QCB) Sandbox, operating "
    "within its regulatory framework (and QFC-registered). Madad helps SMEs "
    "unlock working capital tied up in unpaid invoices owed by enterprise or "
    "government buyers: the business uploads an invoice, Madad assesses the "
    "business, and connects it with credit-line offers from multiple trusted "
    "financial institutions — faster and simpler than traditional financing.\n\n"
    "TERMS you may be asked to explain (explain clearly, in general terms):\n"
    "- Tenure: the financing period before the buyer's (paymaster's) repayment "
    "is expected, e.g. 30 vs 45 days. A SHORTER tenure usually means lower total "
    "profit/fee cost (good when the buyer pays quickly); a LONGER tenure gives "
    "more flexibility, and sometimes a higher limit, if the buyer takes longer "
    "to pay.\n"
    "- Profit rate / p.a.: the annualised cost of the financing.\n"
    "- Fee: the one-time processing charge shown on an offer.\n"
    "- Credit line / limit: the maximum amount a lender offers.\n\n"
    "HOW TO ANSWER: read the message and answer directly, warmly and briefly — "
    "1 to 3 short WhatsApp-style sentences; an emoji is fine. NEVER invent "
    "specific rates, limits, fees, approvals, timelines or offer numbers — speak "
    "generally and point them to their Madad account for exact figures. If they "
    "ask why a document or detail is needed, explain simply that it verifies the "
    "business and assesses financing eligibility. For account-specific status you "
    "don't know, reassure them the team is reviewing and it will update soon.\n\n"
    "CRITICAL — STAY IN YOUR LANE: Answer ONLY the question the user asked, then "
    "STOP. Do NOT tell the user what to do next, do NOT ask them to upload, share "
    "or provide anything, do NOT ask follow-up questions, and do NOT describe, "
    "invent, or assume any application step or order — a separate system guides "
    "them to the correct next step right after your reply. Never restate the step "
    "or say things like 'shall I guide you' or 'what type of business do you "
    "have'. Never include a phone number.\n\n"
    "GUARDRAILS: Be genuinely helpful. You CAN answer anything reasonably "
    "related to Madad, business or invoice financing, the application/onboarding "
    "process, the documents we ask for (what each one is, why it's needed, what "
    "file formats are accepted like PDF or a clear photo, and broadly HOW or "
    "WHERE a Qatar business owner can obtain them — e.g. a CR from the Ministry "
    "of Commerce and Industry), and account/status questions. Answer these "
    "practically, even basic ones like 'what is an email' or 'how do I get my "
    "CR'. ONLY decline if the message is CLEARLY UNRELATED to Madad or business "
    "financing (general trivia, math problems, coding, recipes, news, politics, "
    "sport) or is abusive/profane — then reply with ONE polite sentence like: "
    "\"That's outside what I can help with here — I'm your Madad onboarding "
    "assistant, so feel free to ask me anything about your application or "
    "financing 🙂\" and do not answer it. Never produce profanity or offensive "
    "content."
)


async def _llm_answer(user_text: str, step_hint: str) -> str | None:
    """Return a contextual LLM reply, or ``None`` if unavailable/failed.

    Reads OpenAI config from the environment (same key the MCP cluster uses).
    Any error (no key, network, bad response) yields ``None`` so the caller can
    fall back to a canned line — the onboarding flow must never break on this.
    """

    text = (user_text or "").strip()
    if not text:
        return None
    # Prefer Groq (OpenAI-compatible, free, fast) when its key is present —
    # configured independently so a stale OPENAI_MODEL (e.g. gpt-4.1-mini) can't
    # leak into a Groq call. Falls back to OpenAI config, then to None (canned
    # reply) so the onboarding flow never breaks on an LLM hiccup.
    groq_key = os.getenv("GROQ_API_KEY", "").strip()
    if groq_key:
        api_key = groq_key
        base_url = (
            os.getenv("GROQ_BASE_URL", "https://api.groq.com/openai/v1")
            .strip()
            .rstrip("/")
        )
        model = (
            os.getenv("GROQ_MODEL", "llama-3.3-70b-versatile").strip()
            or "llama-3.3-70b-versatile"
        )
    else:
        api_key = os.getenv("OPENAI_API_KEY", "").strip()
        base_url = (
            os.getenv("OPENAI_BASE_URL", "https://api.openai.com/v1").strip().rstrip("/")
        )
        model = os.getenv("OPENAI_MODEL", "gpt-4.1-mini").strip() or "gpt-4.1-mini"
    if not api_key:
        return None
    try:
        timeout = float(os.getenv("LLM_TIMEOUT", os.getenv("OPENAI_TIMEOUT", "15")) or "15")
    except ValueError:
        timeout = 15.0
    payload = {
        "model": model,
        "temperature": 0.4,
        "max_tokens": 220,
        "messages": [
            {"role": "system", "content": _SMART_SYSTEM_PROMPT},
            {
                "role": "system",
                "content": f"The current onboarding step expects: {step_hint}",
            },
            {"role": "user", "content": text[:1500]},
        ],
    }
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            resp = await client.post(
                f"{base_url}/chat/completions",
                headers={"Authorization": f"Bearer {api_key}"},
                json=payload,
            )
            resp.raise_for_status()
            data = resp.json()
        answer = (data["choices"][0]["message"]["content"] or "").strip()
        return answer or None
    except Exception:  # noqa: BLE001 — never break the flow on an LLM hiccup
        return None


def _valid_upload_attachments(value: Any) -> list[dict[str, Any]]:
    """Return only attachments with actual bytes for backend upload.

    A provider/media id alone is not enough for this workflow because the KYC
    MCP tool expects base64 file bytes. The Madad backend WhatsApp bridge
    downloads Meta media and forwards ``content_base64``; if that field is
    missing or empty, keep the user on the same upload step.
    """

    attachments = reply_attachments(value)
    return [
        attachment
        for attachment in attachments
        if str(attachment.get("content_base64") or "").strip()
    ]


def _is_zip_attachment(attachment: dict[str, Any]) -> bool:
    """True if the attachment is a ZIP archive (by mime type or extension)."""

    mime = str(attachment.get("mime_type") or "").lower()
    if "zip" in mime:
        return True
    filename = str(attachment.get("filename") or "").lower()
    return filename.endswith(".zip")


def _guess_mime_for(filename: str) -> str:
    lowered = filename.lower()
    if lowered.endswith(".pdf"):
        return "application/pdf"
    if lowered.endswith((".png",)):
        return "image/png"
    if lowered.endswith((".jpg", ".jpeg")):
        return "image/jpeg"
    if lowered.endswith((".tif", ".tiff")):
        return "image/tiff"
    return "application/octet-stream"


def _expand_zip_attachments(
    attachments: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], bool]:
    """Unpack any ZIP attachments into their member files.

    SMEs are asked to send "all documents in one ZIP" — Meta delivers it as a
    single ``application/zip`` attachment with base64 bytes. We unzip it
    in-memory and emit one synthetic attachment per member file (each carrying
    its own base64 + guessed mime) so the existing per-document upload + classify
    pipeline runs over every file exactly as if they were sent individually.

    Non-ZIP attachments pass through untouched. Returns ``(expanded, saw_zip)``.
    Degrades gracefully: a corrupt/unreadable ZIP is kept as-is (saw_zip stays
    False) so the flow still acknowledges *something* rather than dropping it.
    """

    expanded: list[dict[str, Any]] = []
    saw_zip = False
    for attachment in attachments:
        if not _is_zip_attachment(attachment):
            expanded.append(attachment)
            continue
        try:
            raw = base64.b64decode(str(attachment.get("content_base64") or ""))
            with zipfile.ZipFile(io.BytesIO(raw)) as archive:
                members = [
                    info
                    for info in archive.infolist()
                    if not info.is_dir()
                    and not info.filename.startswith("__MACOSX/")
                    and not info.filename.rsplit("/", 1)[-1].startswith(".")
                ]
                if not members:
                    expanded.append(attachment)
                    continue
                saw_zip = True
                for info in members:
                    member_name = info.filename.rsplit("/", 1)[-1]
                    member_bytes = archive.read(info)
                    expanded.append(
                        {
                            "filename": member_name,
                            "content_base64": base64.b64encode(member_bytes).decode(
                                "ascii"
                            ),
                            "mime_type": _guess_mime_for(member_name),
                        }
                    )
        except (zipfile.BadZipFile, binascii.Error, ValueError, OSError):
            # Not a real/readable ZIP — treat it as an ordinary attachment.
            expanded.append(attachment)
    return expanded, saw_zip


def _extract_email(text: str) -> str | None:
    match = re.search(r"[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}", text)
    return match.group(0) if match else None


def _parse_buyer_text(text: str) -> dict[str, Any]:
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    if not lines:
        return {}
    contact_email = _extract_email(text)
    return {
        "name": lines[0],
        **({"contact_email": contact_email} if contact_email else {}),
    }


def _parse_eligibility_text(text: str) -> dict[str, Any]:
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    lowered = text.lower()
    if len(lines) < 5 or lowered.strip() in {"yes", "no", "y", "n"}:
        return {}

    def first_number(value: str) -> str | None:
        match = re.search(r"\d+(?:\.\d+)?", value)
        return match.group(0) if match else None

    business_age = DEFAULT_ELIGIBILITY_FORM["business_age"]
    age_number = first_number(text)
    if age_number is not None:
        age = float(age_number)
        if age >= 5:
            business_age = "OVER_5_YEARS"
        elif age > 2:
            business_age = "OVER_2_YEARS_UNDER_5"

    company_type = DEFAULT_ELIGIBILITY_FORM["company_type"]
    if "sole" in lowered:
        company_type = "SOLE"
    elif "partner" in lowered:
        company_type = "PARTNERSHIP"
    elif "llc" in lowered:
        company_type = "LLC"

    turnover = first_number(lines[5]) if len(lines) > 5 else None
    employees = first_number(lines[6]) if len(lines) > 6 else None

    return {
        # The CR step already verified Qatar registration, and the free-text
        # questionnaire answer ("1. Yes ...") doesn't reliably start with "yes";
        # treat as Qatar-based unless the user explicitly says no/not.
        "is_qatar_based": not any(
            neg in lowered.splitlines()[0].lower() for neg in ("no", "not")
        ),
        "business_age": business_age,
        "cr_validity": "UNDER_1_MONTH" if "valid" in lowered else "OVER_3_MONTHS",
        "company_type": company_type,
        "sector": lines[4] if len(lines) > 4 else DEFAULT_ELIGIBILITY_FORM["sector"],
        "turnover": turnover or DEFAULT_ELIGIBILITY_FORM["turnover"],
        "employees": employees or DEFAULT_ELIGIBILITY_FORM["employees"],
    }


def _parse_shareholder_text(text: str, fallback_phone: str | None) -> list[dict[str, Any]]:
    cleaned = text.strip()
    if not cleaned:
        return []
    first_line = cleaned.splitlines()[0].strip()
    name = re.sub(r"\b\d+(?:\.\d+)?\s*%?\b", "", first_line).strip(" ,-")
    if not name:
        name = first_line
    return [{"name": name, "phoneNumber": fallback_phone or "+97400000000"}]


# Bug #11 (UAT 2026-06-09): debounce window for the documents.processing
# ack. Madad's bridge POSTs each ZIP-member as a separate inbound; without
# the debounce we sent 8 "📦 Got it — processing your documents now…"
# messages in 3 seconds. 30s covers a typical multi-file burst plus a small
# buffer; anything older is treated as a fresh batch and re-fires the ack.
DOCS_PROCESSING_ACK_TTL_SECONDS = 30.0
# Single isolated file → prompt inline immediately (like a ZIP), no 45s sweep
# wait (user 2026-06-13). A wave counts as isolated when it carries one
# attachment AND the previous upload was either the first ever or this far in
# the past — i.e. NOT part of a rapid bulk burst (those still defer to the
# settle sweep so we don't fire one checklist per file).
DOCS_SINGLE_INLINE_GAP_SECONDS = 90.0
# The "any more documents?" prompt is debounced to once per upload burst — a
# ZIP / multi-doc upload arrives as many separate inbounds and must not yield
# one prompt per file (user 2026-06-12).
MORE_DOCS_PROMPT_TTL_SECONDS = 45.0
# A multi-file WhatsApp upload arrives as many inbound waves a few seconds apart.
# Treat a pause LONGER than this between waves as a NEW upload session, so the
# checklist + "any more?" prompt fires once per session (not per wave) but does
# re-appear if the SME uploads a fresh batch after a break (UAT 2026-06-13).
DOCS_SESSION_GAP_SECONDS = 30.0

# Bug #15 (UAT 2026-06-09): same debounce shape but for the full
# "📋 Application checklist" body. Per-file POSTs land as separate
# inbounds; without the debounce the SME got the 15-line ✅/⚠️
# checklist after EVERY file upload. 60s is longer than the processing
# ack so the SME sees one full checklist per upload session — the
# per-file brief receipt (✅ X — Received & Validated) still fires
# every time so progress is visible.
DOCS_CHECKLIST_TTL_SECONDS = 60.0


def _parse_iso_or_none(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except (TypeError, ValueError):
        return None


DEFAULT_WHATSAPP_REQUIRED_DOCS = [
    # Business documents (Trade License + Tax Card are NOT collected by Madad)
    "national_address_certificate",
    "article_of_association",
    "establishment_card",
    # Financial documents (Audited Financial Statement already collected earlier)
    "credit_bureau_report",
    "payable_ageing",
    "receivable_ageing",
    "interim_statement",
    "bank_statement",
    # Shareholder documents
    "qid",
    "passport",
]

# Per user 2026-06-10: optional shareholder docs the SME MAY send but MUST NOT
# block completion on. If uploaded the agent acknowledges + validates them like
# any required doc; if never sent, the docs loop still naturally exhausts when
# every entry in ``DEFAULT_WHATSAPP_REQUIRED_DOCS`` is in. See
# [[project_optional_docs]].
DEFAULT_WHATSAPP_OPTIONAL_DOCS = [
    "proof_of_address",
]

DOCUMENT_LABELS = {
    "trade_license": "Trade License",
    "tax_card": "Tax Card",
    "national_address_certificate": "National Address Certificate",
    "article_of_association": "Article of Association",
    "establishment_card": "Establishment Card",
    "bank_statement": "Bank Statement (last 6 months)",
    "audited_report": "Audited Report",
    "audited_financial_report": "Audited Financial Statement",
    "credit_bureau_report": "Qatar Credit Bureau Report",
    "payable_ageing": "Payable Ageing Schedule",
    "receivable_ageing": "Receivable Ageing Schedule",
    "interim_statement": "Interim Financial Statement",
    "qid": "Shareholder QID",
    "passport": "Shareholder Passport",
    "proof_of_address": "Shareholder Proof of Address",
}

# Filename → KYC document_type inference for the documents upload loop. Ordered
# loosely most-specific-first; the first keyword found in the (lowercased)
# filename wins. Covers every code in DEFAULT_WHATSAPP_REQUIRED_DOCS so a ZIP of
# sensibly-named files (e.g. ArticleOfAssociation.pdf, QID_Shareholder2.pdf)
# classifies without the next-pending fallback.
DOC_TYPE_KEYWORDS = {
    "trade": "trade_license",
    "tax": "tax_card",
    "national address": "national_address_certificate",
    "nationaladdress": "national_address_certificate",
    "address cert": "national_address_certificate",
    "article": "article_of_association",
    "aoa": "article_of_association",
    "association": "article_of_association",
    "establishment": "establishment_card",
    "credit bureau": "credit_bureau_report",
    "creditbureau": "credit_bureau_report",
    "bureau": "credit_bureau_report",
    "payable": "payable_ageing",
    "receivable": "receivable_ageing",
    "interim": "interim_statement",
    "bank": "bank_statement",
    "audited": "audited_report",
    "audit": "audited_report",
    "passport": "passport",
    "qid": "qid",
    "national id": "qid",
    "proof of address": "proof_of_address",
    "proofofaddress": "proof_of_address",
    "vat": "vat_certificate",
}


def _infer_doc_type(filename: str) -> str | None:
    lowered = filename.lower()
    for keyword, doc_type in DOC_TYPE_KEYWORDS.items():
        if keyword in lowered:
            return doc_type
    return None


def _format_documents(documents: list[str]) -> str:
    if not documents:
        return "required documents"
    labels = [DOCUMENT_LABELS.get(doc, doc.replace("_", " ").title()) for doc in documents]
    return "\n".join(f"{idx}. {label}" for idx, label in enumerate(labels, start=1))


def _pending_checklist_body(missing: list[str]) -> str:
    """The "📋 Application checklist" body (✅ done + ⚠️ still-needed + optional).

    Returns "" when nothing is missing. Shared by the on-demand pending-docs
    reply and the end-of-upload settle nudge so both render identically.
    """

    def _label(doc: str) -> str:
        return DOCUMENT_LABELS.get(doc, doc.replace("_", " ").title())

    still_missing = list(missing)
    if not still_missing:
        return ""
    all_required = list(DEFAULT_WHATSAPP_REQUIRED_DOCS)
    already_validated = [d for d in all_required if d not in still_missing]
    rows = [f"✅ {_label(d)}" for d in already_validated]
    rows += [f"⚠️ {_label(d)} — still needed" for d in still_missing]
    body = "📋 Application checklist:\n" + "\n".join(rows)
    noun = "document" if len(still_missing) == 1 else "documents"
    body += (
        f"\n\n📤 Please share the remaining {len(still_missing)} {noun} to move forward."
    )
    optional_unsent = [
        d for d in DEFAULT_WHATSAPP_OPTIONAL_DOCS if d not in already_validated
    ]
    if optional_unsent:
        body += "\n\nℹ️ Optional (send if you have them):"
        for d in optional_unsent:
            body += f"\n• {_label(d)}"
    return body


def _format_banks_list(banks: list[str]) -> str:
    """Render the assigned-banks list as natural prose ('A and B', 'A, B and
    C') for the Step 6 payment-confirmed message. Empty list → 'our banking
    partners' so the sentence still reads correctly."""

    cleaned = [b for b in banks if b]
    if not cleaned:
        return "our banking partners"
    if len(cleaned) == 1:
        return cleaned[0]
    if len(cleaned) == 2:
        return f"{cleaned[0]} and {cleaned[1]}"
    return ", ".join(cleaned[:-1]) + f", and {cleaned[-1]}"


def _extract_offers_from_me(info: Any) -> list[dict[str, Any]]:
    """Read the offer list off an ``auth_me`` response.

    Per Ishan's handover §8 (2026-06-09): the field on the enriched
    ``/me`` payload is ``offersReceived``, not ``offers``. Earlier code
    only checked ``info.get("offers")`` and silently rendered empty
    offer cards in the WhatsApp "Exciting news — your financing offers
    are ready!" template (UAT screenshot 2026-06-10). Try every shape
    the backend is known to use so a one-off field rename can't drop
    offers on the floor again.
    """

    if not isinstance(info, dict):
        return []
    for owner in (info, info.get("user") if isinstance(info.get("user"), dict) else None):
        if not isinstance(owner, dict):
            continue
        for key in ("offersReceived", "offers", "offer_list"):
            raw = owner.get(key)
            if isinstance(raw, list):
                return [o for o in raw if isinstance(o, dict)]
    return []


def _extract_reference_from_me(info: Any) -> str | None:
    """Read the application reference number off ``auth_me``.

    The backend exposes the same value under several field names across
    `user.uniqueId` / `user.referenceNumber` / top-level `referenceNumber`.
    Falls back to None when none are populated so callers can keep their
    existing ``or state.application_ref`` chain."""

    if not isinstance(info, dict):
        return None
    candidates = (info, info.get("user") if isinstance(info.get("user"), dict) else None)
    for owner in candidates:
        if not isinstance(owner, dict):
            continue
        for key in (
            "referenceNumber", "reference_number",
            "uniqueId", "unique_id", "applicationRef", "application_ref",
        ):
            value = owner.get(key)
            if isinstance(value, str) and value:
                return value
    return None


def _extract_credit_line_from_me(info: Any) -> dict[str, Any]:
    """Read the active credit line off ``auth_me``.

    Per Ishan's handover §8: ``creditLines`` is a list (one per active
    line). For the activation message (PDF Step 9) we want the most
    recent active entry. Returns an empty dict when none are present so
    the caller can fall back to its existing offer-derived fields.
    """

    if not isinstance(info, dict):
        return {}
    candidates = (info, info.get("user") if isinstance(info.get("user"), dict) else None)
    for owner in candidates:
        if not isinstance(owner, dict):
            continue
        raw = owner.get("creditLines") or owner.get("credit_lines")
        if isinstance(raw, list) and raw:
            for entry in raw:
                if isinstance(entry, dict):
                    status = str(
                        entry.get("status") or entry.get("state") or ""
                    ).upper()
                    if not status or "ACTIVE" in status:
                        return entry
            first = raw[0]
            if isinstance(first, dict):
                return first
    return {}


def _offers_sig(offers: list[dict[str, Any]]) -> str:
    """Stable signature of the offer set (lender + key terms), so we re-send the
    offer cards only when a lender actually adds/changes an offer — not on every
    routine status poll while the SME is deciding."""
    parts = sorted(
        f"{_lender_name(o) or '?'}|{o.get('creditLimit') or o.get('credit_limit')}"
        f"|{o.get('interestRate') or o.get('interest_rate')}"
        f"|{o.get('tenureDays') or o.get('tenure_days')}"
        for o in (offers or [])
        if isinstance(o, dict)
    )
    return ";".join(parts)


def _selected_offer_from_payload(payload: Any) -> dict[str, Any] | None:
    """Pull the lender + terms off an offer.selected / credit_line.activated
    webhook payload so the confirmation/activation messages can name the bank."""
    if not isinstance(payload, dict):
        return None
    name = payload.get("lenderName") or payload.get("bankName")
    if not name:
        return None
    return {
        "lenderName": name,
        "creditLimit": payload.get("creditLimit"),
        "interestRate": payload.get("interestRate"),
        "tenureDays": payload.get("tenureDays"),
        "currency": payload.get("currency"),
    }


def _lender_name(offer: dict[str, Any]) -> str | None:
    """Extract a lender's display NAME from an offer across every shape the
    backend uses: ``lenderName`` (webhook payload), ``lender`` as a plain
    string, or ``lender`` / ``financialInstitution`` as a nested object with a
    ``name``. Prevents rendering the raw ``{id, name}`` object in the message.
    """
    if not isinstance(offer, dict):
        return None
    for key in ("lenderName", "bankName", "lender_name", "bank_name"):
        v = offer.get(key)
        if isinstance(v, str) and v.strip():
            return v.strip()
    for key in ("lender", "financialInstitution", "bank"):
        v = offer.get(key)
        if isinstance(v, str) and v.strip():
            return v.strip()
        if isinstance(v, dict):
            name = v.get("name") or v.get("displayName")
            if isinstance(name, str) and name.strip():
                return name.strip()
    return None


def _format_offer_cards(offers: list[dict[str, Any]]) -> str:
    """Render the offer set as PDF Step 8-style cards (one block per offer).

    Per Ishan's confirmed schema (2026-06-07): each offer carries
    ``lender, creditLimit, interestRate, tenureDays, processingFee,
    expiryDate``. Fields are camelCase but snake_case copies are tolerated
    so test fixtures with shorthand shapes don't crash.

    Returns the empty string when there are no offers so the template
    placeholder substitutes cleanly to nothing.
    """

    if not offers:
        return ""

    def _g(offer: dict[str, Any], *keys: str) -> Any:
        for k in keys:
            if k in offer and offer[k] is not None:
                return offer[k]
        return None

    def _fmt_qar(value: Any) -> str:
        try:
            return f"QAR {int(value):,}"
        except (TypeError, ValueError):
            return f"QAR {value}" if value is not None else "QAR —"

    def _fmt_pct(value: Any) -> str:
        try:
            return f"{float(value):g}% p.a."
        except (TypeError, ValueError):
            return f"{value} p.a." if value is not None else "—"

    def _fmt_days(value: Any) -> str:
        try:
            return f"{int(value)} days"
        except (TypeError, ValueError):
            return f"{value}" if value is not None else "—"

    lines: list[str] = []
    for idx, offer in enumerate(offers, start=1):
        lender = _lender_name(offer) or "Lender"
        limit = _fmt_qar(_g(offer, "creditLimit", "credit_limit", "limit"))
        rate = _fmt_pct(_g(offer, "interestRate", "interest_rate", "rate"))
        tenure = _fmt_days(_g(offer, "tenureDays", "tenure_days", "tenure"))
        # Total fees = sum of every fee component the lender set (processing +
        # other charges + brokerage + feasibility + other fees/commissions).
        # The backend field is ``processingFeeValue`` (NOT ``processingFee``) —
        # reading the wrong key made every offer show "no fee" even when fees
        # existed (UAT 2026-06-13). Show ONE combined total per the user's ask.
        _fee_total = 0.0
        _saw_fee = False
        for _fee_keys in (
            ("processingFeeValue", "processing_fee_value", "processingFee", "processing_fee", "fee"),
            ("otherCharges", "other_charges"),
            ("brokerageFees", "brokerage_fees"),
            ("feasibilityStudyFees", "feasibility_study_fees"),
            ("otherFeesAndCommissions", "other_fees_and_commissions"),
        ):
            _v = _g(offer, *_fee_keys)
            if _v is None:
                continue
            try:
                _fee_total += float(_v)
                _saw_fee = True
            except (TypeError, ValueError):
                continue
        fee = f"{_fmt_qar(_fee_total)} total fees" if _saw_fee and _fee_total > 0 else "no fee"
        lines.append(
            f"🏦 Offer {idx} — {lender}\n"
            f"💰 {limit} · 📈 {rate} · ⏱ {tenure} · 💳 {fee}"
        )
    return "\n━━━━━━━━━━━━━\n".join(lines)


def _next_step_hint(state: OnboardingState) -> str:
    step = state.history[-1].step if state.history else ""
    if step in {"campaign_send", "campaign_await"}:
        return "Please reply YES if you want to start, or NO to opt out."
    if step in {"business_email_send", "business_email_await"}:
        return "Right now I need your business email address. 📧"
    if step in {"consent_send", "consent_await"}:
        return "Right now I need your Commercial Registration (CR) as a PDF or photo."
    if step in {"eligibility_intake_send", "eligibility_intake_await"}:
        return "Right now I need the 7 quick business details: Qatar-based, business age, CR validity, company type, sector, turnover, and employees."  # noqa: E501
    if step in {"financials_send", "financials_await"}:
        return "Right now I need your latest Audited Financial Statement as a PDF or photo."
    if step in {"buyers_collect_send", "buyers_collect_await"}:
        return "Right now I need your main buyer details: name, country, and contact."
    if step in {"shareholders_collect_send", "shareholders_collect_await"}:
        return "Right now I need shareholder details: name and percentage."
    if step in {"documents_upload_loop_send", "documents_upload_loop_await"}:
        return f"Right now I need these documents:\n{_format_documents(state.missing_documents)}"
    if step in {"payment_send_link", "payment_await"}:
        return "Right now your payment link is ready. Once payment is complete, we will forward your application."  # noqa: E501
    if step in {"documents_complete", "journey_wait_await", "lender_wait_await"}:
        return "Your application is under review. I’ll notify you as soon as there is an update."
    return "I’ll guide you step by step through the application."


def _is_conflict_error(exc: BaseException) -> bool:
    """True if any link in the exception's cause chain reports HTTP 409.

    The MCP client wraps backend HTTP failures as ``MCPError("MCP tool
    failed after N attempt(s)")`` with the underlying ``ToolError`` set as
    ``__cause__``; the upstream status code is embedded in that cause's
    message string (e.g. ``Madad API returned HTTP 409``).
    """
    seen: set[int] = set()
    cur: BaseException | None = exc
    while cur is not None and id(cur) not in seen:
        seen.add(id(cur))
        message = str(cur)
        if "HTTP 409" in message or "status_code\":409" in message:
            return True
        cur = cur.__cause__ or cur.__context__
    return False


class OnboardingWorkflow(WorkflowDefinition):
    name = "onboarding"
    version = 1
    state_schema = OnboardingState

    def __init__(
        self,
        *,
        messenger: Messenger,
        identity: MadadIdentityClient,
        kyc: KycClient,
        payments: MonetizationPaymentClient,
        reminders: Reminders,
    ) -> None:
        self._msg = messenger
        self._identity = identity
        self._kyc = kyc
        self._pay = payments
        self._reminders = reminders

    # -- graph wiring ---------------------------------------------------------

    def build(self, graph: GraphBuilder) -> None:
        nodes: dict[str, Any] = {
            # Step 1: campaign + identification
            "campaign_send": self._campaign_send,
            "campaign_await": self._campaign_await,
            "declined": self._declined,
            "check_contact_send": self._check_contact_send,
            "check_contact_await": self._check_contact_await,
            "domain_blocked": self._domain_blocked,
            "registered_route_send": self._registered_route_send,
            # Returning-user resume: open session → fetch live status → re-enter
            # the exact step the SME left off at (instead of greet-and-end).
            "channel_session_resume": self._channel_session_resume,
            "resume_status_fetch": self._resume_status_fetch,
            "resume_rejected": self._resume_rejected,
            "resume_offer_expired": self._resume_offer_expired,
            "resume_application_open": self._resume_application_open,
            "channel_session_first": self._channel_session_first,
            "channel_session_create_user": self._channel_session_create_user,
            "collect_onboarding_details_send": self._collect_onboarding_details_send,
            "collect_onboarding_details_await": self._collect_onboarding_details_await,
            "complete_onboarding_send": self._complete_onboarding_send,
            "channel_session_second": self._channel_session_second,
            # Step 1b: business email (right after YES / account creation)
            "business_email_send": self._business_email_send,
            "business_email_await": self._business_email_await,
            "business_email_conflict_send": self._business_email_conflict_send,
            # Step 2: consent + CR
            "consent_send": self._consent_send,
            "consent_await": self._consent_await,
            "cr_upload_base64": self._cr_upload_base64,
            # Step 3: eligibility intake
            "eligibility_intake_send": self._eligibility_intake_send,
            "eligibility_intake_await": self._eligibility_intake_await,
            "eligibility_update": self._eligibility_update,
            "not_eligible": self._not_eligible,
            # Step 4: financials
            "financials_send": self._financials_send,
            "financials_await": self._financials_await,
            "financials_upload_base64": self._financials_upload_base64,
            # Step 5–6: docs + counterparties
            "documents_list_fetch": self._documents_list_fetch,
            "buyers_collect_send": self._buyers_collect_send,
            "buyers_collect_await": self._buyers_collect_await,
            "shareholders_collect_send": self._shareholders_collect_send,
            "shareholders_collect_await": self._shareholders_collect_await,
            "documents_upload_loop_send": self._documents_upload_loop_send,
            "documents_upload_loop_await": self._documents_upload_loop_await,
            "documents_complete": self._documents_complete,
            # Postman-triggered gates (demo): pre-qualification (after audit)
            # and payment (after coffee) are released by an external trigger.
            "prequalify_wait_await": self._prequalify_wait_await,
            "payment_wait_await": self._payment_wait_await,
            # Step 7: status poll + payment
            "status_poll_on_demand": self._status_poll_on_demand,
            "journey_wait_await": self._journey_wait_await,
            "not_qualified": self._not_qualified,
            "business_details_fetch": self._business_details_fetch,
            "products_list_fetch": self._products_list_fetch,
            "payment_create": self._payment_create,
            "payment_send_link": self._payment_send_link,
            "payment_await": self._payment_await,
            # Step 8: lender + offers + terminals
            "lender_status_poll": self._lender_status_poll,
            "lender_wait_await": self._lender_wait_await,
            "offers_fetch": self._offers_fetch,
            "offer_view_send": self._offer_view_send,
            "offer_handoff_to_madad": self._offer_handoff_to_madad,
            "offer_confirmed": self._offer_confirmed,
            "activated": self._activated,
            "invoice_collect_await": self._invoice_collect_await,
        }
        for node_name, fn in nodes.items():
            graph.add_node(node_name, fn)

        graph.set_entry("campaign_send")
        graph.add_edge("campaign_send", "campaign_await")
        graph.add_conditional_edges(
            "campaign_await",
            self._route_entry,
            {
                "check_contact": "check_contact_send",
                "declined": "declined",
                "ask_again": "campaign_await",
            },
        )
        graph.add_edge("check_contact_send", "check_contact_await")
        graph.add_conditional_edges(
            "check_contact_await",
            self._route_check_contact,
            {
                "existing": "channel_session_first",
                "new": "complete_onboarding_send",
                # WhatsApp organic-entry: single create_user_if_missing call
                # mints SIGN_UP + access_token in one round-trip.
                "new_whatsapp": "channel_session_create_user",
                "blocked": "domain_blocked",
                # Per Ishan (cluster e6ea5d2, 2026-06-10): returning users
                # routed by check_registration are existing accounts mid/post
                # journey — open a session (mint access_token) then RESUME at
                # their exact step via resume_status_fetch, instead of the old
                # greet-and-end. Closes Bug #2 + Bug #6 and the "bot doesn't
                # continue the journey" gap (user 2026-06-12). NOTE: the plain
                # "existing" path (contact exists but check_registration gave no
                # route) keeps the original continue-at-consent behaviour.
                "registered_routed": "channel_session_resume",
            },
        )

        # Plain existing-user path converges at consent_send via one session call
        # (unchanged — early-stage contacts continue onboarding from consent/CR).
        graph.add_conditional_edges(
            "channel_session_first",
            self._route_channel_session,
            {"consent": "consent_send"},
        )
        # Returning-user RESUME path: a dedicated session call mints the
        # access_token, then resume_status_fetch reads the live journey_status
        # and drops the run back into the exact node the SME left off at.
        graph.add_edge("channel_session_resume", "resume_status_fetch")
        graph.add_conditional_edges(
            "resume_status_fetch",
            self._route_resume_by_status,
            {
                # SIGN_UP / ONBOARDED: no email yet → collect it; else consent/CR.
                "email": "business_email_send",
                "consent": "consent_send",
                # ELIGIBLE: right after CR → ask for the audited financials.
                "financials": "financials_send",
                # INCOMPLETE / UNVERIFIED / VERIFIED / PRE_QUALIFIED: all the
                # document-submission phase — the checklist loop asks for what's
                # missing or shows the "under review" coffee message when full.
                "documents": "documents_list_fetch",
                # QUALIFIED: initiate the onboarding-fee payment.
                "payment": "business_details_fetch",
                "offers": "offers_fetch",
                "offer_confirmed": "offer_confirmed",
                "activated": "activated",
                "rejected": "resume_rejected",
                "offer_expired": "resume_offer_expired",
                "application_open": "resume_application_open",
                "ineligible": "not_eligible",
                "unqualified": "not_qualified",
                # Unknown / unreadable status → safe greet-and-end fallback.
                "welcome": "registered_route_send",
            },
        )
        # New-lead path: the spec says NO form-filling — we never ask the SME to
        # type their name/CR/role. The account is created up front with safe
        # placeholders (complete_onboarding fills sensible defaults) and the real
        # business details are extracted from the CR document they upload next.
        # NEW: right after the account exists we collect the BUSINESS email
        # (business_email_send) before consent/CR — this makes the lead a
        # normal, portal-loginable user and catches a duplicate business early.
        graph.add_edge("complete_onboarding_send", "channel_session_second")
        graph.add_conditional_edges(
            "channel_session_second",
            self._route_channel_session,
            {"consent": "business_email_send"},
        )
        # WhatsApp organic-entry: create_user_if_missing fast-path -> email step.
        graph.add_conditional_edges(
            "channel_session_create_user",
            self._route_channel_session,
            {"consent": "business_email_send"},
        )

        # Business-email step. proceed -> consent/CR; conflict -> ask again;
        # await_again -> keep waiting for a valid email.
        graph.add_edge("business_email_send", "business_email_await")
        graph.add_conditional_edges(
            "business_email_await",
            self._route_business_email,
            {
                "proceed": "consent_send",
                "conflict": "business_email_conflict_send",
                "await_again": "business_email_await",
            },
        )
        graph.add_edge("business_email_conflict_send", "business_email_await")

        graph.add_edge("consent_send", "consent_await")
        graph.add_conditional_edges(
            "consent_await",
            self._route_consent_upload,
            {"uploaded": "cr_upload_base64", "missing": "consent_await"},
        )
        # Spec Step 2: straight after the CR we ask for the audited financials.
        # The quick eligibility questionnaire is NOT in the PDF — we treat the
        # business as eligible (Qatar registration is verified from the CR) and
        # skip it entirely.
        graph.add_edge("cr_upload_base64", "financials_send")

        graph.add_edge("financials_send", "financials_await")
        graph.add_conditional_edges(
            "financials_await",
            self._route_financials_upload,
            {"uploaded": "financials_upload_base64", "missing": "financials_await"},
        )
        # Spec Step 3 → pre-qualification: after the audited report + account-
        # created message, PARK until the pre-qualification is triggered (via
        # Postman in the demo). On trigger → the document checklist.
        graph.add_edge("financials_upload_base64", "prequalify_wait_await")
        graph.add_conditional_edges(
            "prequalify_wait_await",
            self._route_prequalify_wait,
            {"go": "documents_list_fetch", "wait": "prequalify_wait_await"},
        )
        # Buyer + shareholder ASK steps are intentionally skipped — they are not
        # in the spec PDF (shareholders come from the CR; buyers are collected
        # later at invoice submission). Go straight to the document checklist.
        graph.add_edge("documents_list_fetch", "documents_upload_loop_send")
        graph.add_edge("documents_upload_loop_send", "documents_upload_loop_await")
        graph.add_conditional_edges(
            "documents_upload_loop_await",
            self._route_documents,
            {
                "complete": "documents_complete",
                # Refinement per Ishan (2026-06-09): when admin QUALIFIES
                # mid-docs-loop, jump STRAIGHT to the payment chain —
                # bypass the misleading "all documents received" coffee
                # message (the checklist isn't actually complete) and
                # the now-redundant payment_wait_await stop.
                "payment": "business_details_fetch",
                "missing": "documents_upload_loop_send",
                "await_again": "documents_upload_loop_await",
                # SME replied NO to "any more documents?" → proceed to the
                # payment-wait park even though some docs are still undetected.
                "proceed": "payment_wait_await",
            },
        )
        # Per user (2026-06-12): NO "any more documents?" prompt once the
        # checklist is satisfied — it caused a stuck loop (every non-YES/NO
        # reply, INCLUDING an incoming qualify/offer webhook, re-fired the
        # "No problem…" line) and swallowed the qualify event so the payment
        # message never came. After the coffee message we re-park in the SMART
        # upload-await node, which (a) silently accepts any further document
        # uploads and (b) ALWAYS breaks out to the right message on a
        # QUALIFIED / ACCEPTED / OFFER_ACCEPTED / ACTIVATED status event. The
        # coffee message fires ONCE (guarded by ``documents_complete_sent``).
        graph.add_edge("documents_complete", "documents_upload_loop_await")
        # Spec Step 5 → payment: after the coffee message + more-docs prompt,
        # PARK until the payment step is triggered (via Postman in the demo).
        # On trigger → Madad score + the "Pay QAR 6,000 →" button.
        graph.add_conditional_edges(
            "payment_wait_await",
            self._route_payment_wait,
            {"go": "business_details_fetch", "wait": "payment_wait_await"},
        )

        graph.add_conditional_edges(
            "status_poll_on_demand",
            self._route_journey_status,
            {
                "payment": "business_details_fetch",
                "ineligible": "not_eligible",
                "unqualified": "not_qualified",
                "offers": "offers_fetch",
                "offer_confirmed": "offer_confirmed",
                "activated": "activated",
                "wait": "journey_wait_await",
            },
        )
        graph.add_conditional_edges(
            "journey_wait_await",
            self._route_status_resume,
            {"poll": "status_poll_on_demand", "await_again": "journey_wait_await"},
        )

        # Payment chain: business details → product lookup → create (with
        # idempotency_key) → send link (with idempotency_key) → await.
        graph.add_edge("business_details_fetch", "products_list_fetch")
        graph.add_edge("products_list_fetch", "payment_create")
        graph.add_edge("payment_create", "payment_send_link")
        graph.add_edge("payment_send_link", "payment_await")
        graph.add_conditional_edges(
            "payment_await",
            self._route_payment,
            {"paid": "lender_status_poll", "unpaid": "payment_await"},
        )

        graph.add_conditional_edges(
            "lender_status_poll",
            self._route_journey_status,
            {
                "payment": "lender_wait_await",
                "ineligible": "not_qualified",
                "unqualified": "not_qualified",
                "offers": "offers_fetch",
                "offer_confirmed": "offer_confirmed",
                "activated": "activated",
                "wait": "lender_wait_await",
            },
        )
        graph.add_conditional_edges(
            "lender_wait_await",
            self._route_status_resume,
            {"poll": "lender_status_poll", "await_again": "lender_wait_await"},
        )

        graph.add_edge("offers_fetch", "offer_view_send")
        graph.add_edge("offer_view_send", "offer_handoff_to_madad")
        # Step 8 → 9: after showing offers + the "Login to Madad" button the run
        # must NOT terminate — it parks in journey_wait_await so the post-handoff
        # webhooks can resume it: another lender's offers.available (re-show ALL
        # offers), offer.selected (✅ confirmation), credit_line.activated
        # (activation message). Previously offer_handoff was a finish node, so
        # every one of those events hit a dead run and no message was sent.
        graph.add_edge("offer_handoff_to_madad", "journey_wait_await")
        graph.add_edge("offer_confirmed", "journey_wait_await")

        # Step 9 → 10-13: after the credit-line activation message, the run no
        # longer terminates — it parks to collect invoices for financing. Each
        # uploaded invoice is submitted immediately; the node loops to accept
        # more. (Agent-only graph; portal/other flows are unaffected.)
        graph.add_edge("activated", "invoice_collect_await")
        graph.add_conditional_edges(
            "invoice_collect_await",
            self._route_invoice_collect,
            {"loop": "invoice_collect_await"},
        )

        for terminal in (
            "declined",
            "domain_blocked",
            "not_eligible",
            "not_qualified",
            # Returning-user route per Ishan's check_registration tool —
            # the SME has been re-greeted with the right message, no
            # further onboarding work is needed in this run.
            "registered_route_send",
            # Returning-user resume terminals (status-specific messages that
            # have no in-chat next action — contact support / log in to portal).
            "resume_rejected",
            "resume_offer_expired",
            "resume_application_open",
        ):
            graph.set_finish(terminal)

    # -- Step 1: campaign + check_contact + session ---------------------------

    async def _campaign_send(self, state: OnboardingState, ctx: WorkflowContext) -> dict[str, Any]:
        locale = str(state.data.get("locale") or state.locale)
        # Step 0 reach-out. This is the FIRST outbound to the contact, so there
        # is no open 24h window — Meta rejects free text. It MUST be the
        # approved "initiate" WhatsApp template. Fall back to the CMS free-text
        # intro only for non-WhatsApp channels (or if the template send fails),
        # mirroring the CTA-button fallback pattern.
        sent_as_template = False
        if ctx.channel is Channel.WHATSAPP:
            try:
                sent_as_template = await self._msg.send_template(
                    channel=_channel(ctx),
                    identity=ctx.identity,
                    template_name="initiate",
                    template_key="onboarding.campaign.intro",
                    # Per locale-propagation contract: thread state.locale
                    # through to Meta's language_code so Arabic / English
                    # template variants resolve correctly.
                    language_code=locale,
                )
            except Exception as exc:  # noqa: BLE001 — fall back to free text
                ctx.logger.warning("campaign_send.template_failed", error=str(exc)[:200])
        if not sent_as_template:
            await self._send(ctx, state, "onboarding.campaign.intro", locale=locale)
        return self._step("campaign_send", ctx, locale=locale)

    async def _campaign_await(self, state: OnboardingState, ctx: WorkflowContext) -> dict[str, Any]:
        reply = await_input({"waiting_for": "reply", "step": "campaign"})
        help_template = _off_script_template(reply)
        if help_template is not None:
            await self._send(
                ctx,
                state,
                "onboarding.help.contextual",
                {"answer": self._answer_for(help_template), "next_step": _next_step_hint(state)},
            )
            await self._send(ctx, state, "onboarding.campaign.awaiting_yes_no")
            return self._step("campaign_await", ctx, entry_reply="ASK")
        if is_yes(reply):
            entry_reply = "YES"
        elif is_no(reply):
            entry_reply = "NO"
        else:
            # A question/chat before they say YES/NO — answer it (Groq) and end
            # on the YES/NO nudge, instead of robotically re-asking. The Groq
            # fallback default already carries the YES/NO prompt. (user 2026-06-14)
            await self._contextual_off_script(
                ctx, state, reply,
                default_answer="Are you interested in financing for your business? "
                "Please reply YES or NO.",
            )
            entry_reply = "ASK"
        return self._step("campaign_await", ctx, entry_reply=entry_reply)

    async def _declined(self, state: OnboardingState, ctx: WorkflowContext) -> dict[str, Any]:
        await self._send(ctx, state, "onboarding.declined")
        return self._step("declined", ctx, outcome="declined")

    async def _check_contact_send(
        self, state: OnboardingState, ctx: WorkflowContext
    ) -> dict[str, Any]:
        if ctx.channel is Channel.WHATSAPP:
            result = await self._identity.check_contact(phone=ctx.identity)
        else:
            result = await self._identity.check_contact(email=ctx.identity)
        # Per Ishan (cluster commit e6ea5d2, 2026-06-10): also fire the
        # read-only registration lookup — it returns the full registered
        # shape (route hint, journey status, fee paid, credit line,
        # offers…) so the dispatcher can skip SIGN_UP for returning
        # users and re-send the appropriate message instead of
        # silently re-onboarding (Bug #2 + Bug #6). Best-effort: a
        # failure here must not break the SIGN_UP path.
        route: str | None = None
        payload: dict[str, Any] = {}
        try:
            reg = await self._identity.check_registration(
                identifier=ctx.identity,
                channel=_channel(ctx),
            )
            if isinstance(reg, dict) and reg.get("registered"):
                payload = reg
                raw_route = reg.get("route")
                if isinstance(raw_route, str):
                    route = raw_route
        except Exception as exc:  # noqa: BLE001 — non-fatal, fall through
            ctx.logger.warning(
                "check_registration.failed", error=str(exc)[:200]
            )
        return self._step(
            "check_contact_send",
            ctx,
            check_contact_result=result,
            channel_identity=ctx.identity,
            registration_route=route,
            registration_payload=payload,
        )

    async def _check_contact_await(
        self, state: OnboardingState, ctx: WorkflowContext
    ) -> dict[str, Any]:
        # Passthrough — the lookup happens in _check_contact_send and the
        # router branches on its result; no inbound input is awaited here.
        return self._step("check_contact_await", ctx)

    async def _domain_blocked(
        self, state: OnboardingState, ctx: WorkflowContext
    ) -> dict[str, Any]:
        domain = state.check_contact_result.domain if state.check_contact_result else None
        await self._send(
            ctx, state, "onboarding.domain_blocked", {"domain": domain or ""}
        )
        return self._step(
            "domain_blocked",
            ctx,
            outcome="domain_blocked",
            domain_block_reason=domain,
        )

    async def _registered_route_send(
        self, state: OnboardingState, ctx: WorkflowContext
    ) -> dict[str, Any]:
        """Returning-user landing per Ishan's check_registration route.

        Cluster commit ``e6ea5d2`` (2026-06-10): a single
        ``madad_mcp_check_registration`` call before SIGN_UP returns a
        ``route`` enum that says which message to send to a returning user
        — portal login, payment link re-send, offers re-display, invoice
        upload invite, etc. Closes Bug #2 (existing account not detected
        at sign-up) and Bug #6 (portal login creating a duplicate).

        This node picks a friendly route-specific answer and completes the
        run — we don't restart onboarding for someone whose application is
        already in flight on the portal / awaiting payment / has offers in
        flight. The full payload remains on ``state.registration_payload``
        for any downstream node that wants the credit-line / offer details.
        """

        route = state.registration_route or "continue_step"
        payload = state.registration_payload or {}
        ref = payload.get("referenceNumber") or ""

        answer_by_route: dict[str, str] = {
            "portal_login_required": (
                "👋 Welcome back! Your application is being managed on the "
                "Madad portal. Please log in at uat-portal.madadfintech.com to continue."
                + (f" (Ref: {ref})" if ref else "")
            ),
            "invoice_discounting": (
                "🎉 Welcome back! Your credit line is already active — "
                "send any invoice you'd like to finance here as a PDF or "
                "photo and I'll submit it right away."
            ),
            "offer_accepted_confirmation": (
                "✅ Welcome back! You've already accepted an offer with us — "
                "our team is coordinating the next steps and we'll be in "
                "touch shortly."
            ),
            "offers_available": (
                "🎉 Welcome back — your financing offers are ready to "
                "review. Log in at uat-portal.madadfintech.com to compare them side "
                "by side and pick the one you want."
            ),
            "payment_received": (
                "💚 Welcome back! Your onboarding fee is already in and "
                "your application is with our banking partners — we'll "
                "ping you the moment they respond (typically 3–5 business "
                "days)."
            ),
            "payment_link": (
                "👋 Welcome back! You're qualified for financing — please "
                "complete the QAR 6,000 onboarding fee to forward your "
                "application to the banks. Log in at uat-portal.madadfintech.com to "
                "pay or reply 'pay' and I'll re-send the link."
            ),
            "continue_step": (
                "👋 Welcome back! Picking up where you left off — share "
                "the next document we asked for whenever you're ready, "
                "or reply 'list' to see what's still pending."
            ),
        }
        answer = answer_by_route.get(route, answer_by_route["continue_step"])
        await self._send(
            ctx, state, "onboarding.help.contextual",
            {"answer": answer, "next_step": ""},
        )
        return self._step(
            "registered_route_send",
            ctx,
            outcome="returning_user",
            application_ref=ref or state.application_ref,
        )

    # -- Returning-user RESUME (continue the journey at the exact step) -------

    async def _channel_session_resume(
        self, state: OnboardingState, ctx: WorkflowContext
    ) -> dict[str, Any]:
        """Returning user (check_registration returned a route): open a channel
        session to mint a fresh access_token, then resume_status_fetch reads the
        live journey_status off it. Mirrors _channel_session_first but feeds the
        RESUME branch instead of consent/CR."""
        session = await self._identity.open_session(
            channel=_channel(ctx),
            identifier=ctx.identity,
            create_onboarding_token=False,
        )
        # Prefer the authoritative referenceNumber from check_registration's
        # payload (the SME's real application ref) over the freshly-minted
        # session ref, so status queries / payment re-sends use the right one.
        ref = (state.registration_payload or {}).get("referenceNumber")
        return self._step(
            "channel_session_resume",
            ctx,
            channel_session_response=session,
            session_type=session.session_type,
            access_token=session.access_token,
            refresh_token=session.refresh_token,
            token_expires_at=session.token_expires_at,
            madad_user_id=session.user_or_lead_ref,
            application_ref=ref or session.reference_number or state.application_ref,
        )

    async def _resume_status_fetch(
        self, state: OnboardingState, ctx: WorkflowContext
    ) -> dict[str, Any]:
        """Returning user: read the authoritative ``journeyStatus`` (and whether
        the account has an email) so ``_route_resume_by_status`` can drop the
        LIVE run back into the exact step the SME left off at — rather than the
        old greet-and-end behaviour. ``channel_session_first`` has just minted
        the ``access_token`` we poll with. Tolerant: any failure leaves
        ``journey_status`` unset and the router falls back to a safe greeting.
        """
        status = await self._poll_journey_status(state)
        has_email: bool | None = None
        if state.access_token:
            try:
                info = await self._identity.me(access_token=state.access_token)
                if isinstance(info, dict):
                    nested = info.get("user")
                    user = nested if isinstance(nested, dict) else info
                    has_email = bool(user.get("email"))
            except Exception as exc:  # noqa: BLE001 — tolerate; default routing
                ctx.logger.warning(
                    "resume_status_fetch.me_failed", error=str(exc)[:200]
                )
        return self._step(
            "resume_status_fetch",
            ctx,
            journey_status=status,
            account_has_email=has_email,
        )

    def _route_resume_by_status(self, state: OnboardingState) -> str:
        """Map the canonical journey status → the node that re-enters the SME's
        current step. Spec confirmed with the user (2026-06-12), aligned to the
        MCP cluster README's 16-status reference. INCOMPLETE/UNVERIFIED/
        VERIFIED/PRE_QUALIFIED are all the document-submission phase (the loop
        asks for missing docs or shows the under-review message); payment is
        triggered at QUALIFIED, not before."""
        s = state.journey_status
        JS = JourneyStatus
        if s is None:
            return "welcome"
        if s in (JS.SIGN_UP, JS.ONBOARDED):
            return "consent" if state.account_has_email else "email"
        if s == JS.ELIGIBLE:
            return "financials"
        if s in (JS.INCOMPLETE, JS.UNVERIFIED, JS.VERIFIED, JS.PRE_QUALIFIED):
            return "documents"
        if s == JS.QUALIFIED:
            return "payment"
        if s == JS.ACCEPTED:
            return "offers"
        if s == JS.OFFER_ACCEPTED:
            return "offer_confirmed"
        if s == JS.OFFER_EXPIRED:
            return "offer_expired"
        if s == JS.ACTIVATED:
            return "activated"
        if s == JS.NOT_ACCEPTED:
            return "rejected"
        if s == JS.OPEN:
            return "application_open"
        if s == JS.IN_ELIGIBLE:
            return "ineligible"
        if s == JS.UNQUALIFIED:
            return "unqualified"
        return "welcome"

    async def _resume_rejected(
        self, state: OnboardingState, ctx: WorkflowContext
    ) -> dict[str, Any]:
        await self._send(
            ctx, state, "onboarding.help.contextual",
            {
                "answer": (
                    "We're sorry — after review by our banking partners your "
                    "application was not accepted this time. Please contact "
                    "Madad support at support@madadfintech.com or "
                    "madadfintech.com and our team will talk you through your "
                    "options."
                ),
                "next_step": "",
            },
        )
        return self._step("resume_rejected", ctx, outcome="returning_user")

    async def _resume_offer_expired(
        self, state: OnboardingState, ctx: WorkflowContext
    ) -> dict[str, Any]:
        await self._send(
            ctx, state, "onboarding.help.contextual",
            {
                "answer": (
                    "Your financing offer(s) have expired. Please contact Madad "
                    "support at support@madadfintech.com (or madadfintech.com) "
                    "and we'll have fresh offers issued for you."
                ),
                "next_step": "",
            },
        )
        return self._step("resume_offer_expired", ctx, outcome="returning_user")

    async def _resume_application_open(
        self, state: OnboardingState, ctx: WorkflowContext
    ) -> dict[str, Any]:
        await self._send(
            ctx, state, "onboarding.help.contextual",
            {
                "answer": (
                    "Your application is open — we need a little more "
                    "information to proceed. Please log in to the Madad portal "
                    "at uat-portal.madadfintech.com to see what's required, or "
                    "contact support@madadfintech.com and our team will help."
                ),
                "next_step": "",
            },
        )
        return self._step("resume_application_open", ctx, outcome="returning_user")

    async def _channel_session_first(
        self, state: OnboardingState, ctx: WorkflowContext
    ) -> dict[str, Any]:
        session = await self._identity.open_session(
            channel=_channel(ctx),
            identifier=ctx.identity,
            create_onboarding_token=False,
        )
        return self._step(
            "channel_session_first",
            ctx,
            channel_session_response=session,
            session_type=session.session_type,
            access_token=session.access_token,
            refresh_token=session.refresh_token,
            token_expires_at=session.token_expires_at,
            madad_user_id=session.user_or_lead_ref,
            application_ref=session.reference_number or state.application_ref,
        )

    async def _channel_session_create_user(
        self, state: OnboardingState, ctx: WorkflowContext
    ) -> dict[str, Any]:
        """WhatsApp organic-entry: open a session with
        ``create_user_if_missing=True``. Per Ishan (2026-06-07), the backend
        auto-creates a SIGN_UP account from the phone alone and returns an
        ``accessToken`` directly — no separate ``collect_details`` intake or
        ``complete_onboarding`` round-trip required. The ``referenceNumber``
        on the response (``User.uniqueId``) populates immediately so the
        account-created message can surface it on the very next step.
        """

        session = await self._identity.open_session(
            channel=_channel(ctx),
            identifier=ctx.identity,
            create_onboarding_token=False,
            create_user_if_missing=True,
        )
        # Step 1 — lead created (SIGN_UP). Record with the freshly-minted
        # user_id so subsequent progress calls don't need channel+identifier.
        progress_step = await self._update_progress(
            state.model_copy(update={"madad_user_id": session.user_or_lead_ref}),
            ctx,
            step=1,
        )
        return self._step(
            "channel_session_create_user",
            ctx,
            channel_session_response=session,
            session_type=session.session_type,
            access_token=session.access_token,
            refresh_token=session.refresh_token,
            token_expires_at=session.token_expires_at,
            madad_user_id=session.user_or_lead_ref,
            application_ref=session.reference_number or state.application_ref,
            onboarding_progress_step=progress_step or state.onboarding_progress_step,
        )

    async def _collect_onboarding_details_send(
        self, state: OnboardingState, ctx: WorkflowContext
    ) -> dict[str, Any]:
        await self._send(ctx, state, "onboarding.collect_details.request")
        # Open the first bridge call so the new lead has an onboarding_token
        # by the time complete_onboarding_send fires.
        session = await self._identity.open_session(
            channel=_channel(ctx),
            identifier=ctx.identity,
            create_onboarding_token=True,
        )
        return self._step(
            "collect_onboarding_details_send",
            ctx,
            channel_session_response=session,
            session_type=session.session_type,
            onboarding_token=session.onboarding_token,
            madad_user_id=session.user_or_lead_ref,
        )

    async def _collect_onboarding_details_await(
        self, state: OnboardingState, ctx: WorkflowContext
    ) -> dict[str, Any]:
        reply = await_input({"waiting_for": "reply", "step": "collect_details"})
        # The intake supports two shapes:
        #  (a) free-text reply parsed as "First Last" — backward compat;
        #  (b) structured payload with all nine AUTH_COMPLETE_ONBOARDING
        #      fields (the demo runner + web/admin UIs send this).
        data = reply if isinstance(reply, dict) else {}
        text = reply_text(reply)
        lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
        first = str(data.get("first_name") or "")
        last = str(data.get("last_name") or "")
        if not first or not last:
            # Name is ONLY the first line — the rest of the multi-line answer is
            # legal entity / CR / qatar / role and must NOT bleed into the name.
            name_parts = lines[0].split() if lines else []
            first = first or (name_parts[0] if name_parts else "")
            last = last or (" ".join(name_parts[1:]) if len(name_parts) > 1 else "")

        # WhatsApp free-text intake: one line per field after the name —
        # name / legal entity / CR number / Qatar-based / role.
        def _line(i: int) -> str | None:
            return lines[i] if len(lines) > i else None

        legal = data.get("legal_entity_name") or _line(1)
        cr = data.get("cr_number") or _line(2)
        if "is_qatar_based" in data:
            qatar: bool | None = bool(data["is_qatar_based"])
        else:
            line3 = _line(3)
            qatar = (
                line3.lower() in ("yes", "y", "true", "qatar", "qatar based")
                if line3 is not None
                else None
            )
        role = data.get("role") or _line(4)
        return self._step(
            "collect_onboarding_details_await",
            ctx,
            onboarding_first_name=first,
            onboarding_last_name=last,
            onboarding_legal_entity_name=legal,
            onboarding_cr_number=cr,
            onboarding_is_qatar_based=qatar,
            onboarding_role=role,
            onboarding_email_override=data.get("email") or None,
            onboarding_phone_override=data.get("phone") or None,
        )

    async def _complete_onboarding_send(
        self, state: OnboardingState, ctx: WorkflowContext
    ) -> dict[str, Any]:
        # BULLETPROOF account creation. The cluster's complete-onboarding REQUIRES
        # every one of email / legal_entity_name / cr_number / is_qatar_based /
        # role — and the WhatsApp free-text intake may not capture all of them.
        # Fill safe defaults (per-phone where it must stay unique) so the Madad
        # account is ALWAYS created and visible in admin; real values win when set.
        phone = (
            state.onboarding_phone_override
            or (ctx.identity if ctx.channel is Channel.WHATSAPP else None)
        )
        digits = re.sub(r"\D", "", phone or ctx.identity or "") or "demo"
        placeholder_email = f"wa{digits}@wa.madadfintech.com"
        email = (
            state.onboarding_email_override
            or (ctx.identity if ctx.channel is Channel.EMAIL else None)
            or placeholder_email
        )
        first = state.onboarding_first_name or "Madad"
        last = state.onboarding_last_name or "SME"
        legal = state.onboarding_legal_entity_name or f"Madad SME {digits[-6:]}"
        cr = state.onboarding_cr_number or f"WA{digits}"
        qatar = (
            state.onboarding_is_qatar_based
            if state.onboarding_is_qatar_based is not None
            else True
        )
        # role must match an existing backend Role; default to the SME role.
        role = (state.onboarding_role or "").strip().upper().replace(" ", "_")
        if role not in {"TEAM_MEMBER", "AUTHORIZED_SIGNATORY", "SHAREHOLDER"}:
            role = "TEAM_MEMBER"
        try:
            await self._identity.complete_onboarding(
                first_name=first,
                last_name=last,
                onboarding_token=state.onboarding_token or "",
                email=email,
                phone_number=phone,
                legal_entity_name=legal,
                cr_number=cr,
                is_qatar_based=qatar,
                role=role,
            )
        except Exception as exc:  # noqa: BLE001 — degrade in staging
            ctx.logger.warning(
                "complete_onboarding.failed",
                error=str(exc)[:200],
                note="staging-tolerant: continuing — second session call will "
                     "establish identity for the existing user case",
            )
        return self._step("complete_onboarding_send", ctx)

    async def _channel_session_second(
        self, state: OnboardingState, ctx: WorkflowContext
    ) -> dict[str, Any]:
        session = await self._identity.open_session(
            channel=_channel(ctx),
            identifier=ctx.identity,
            create_onboarding_token=False,
        )
        return self._step(
            "channel_session_second",
            ctx,
            channel_session_response=session,
            session_type=session.session_type,
            access_token=session.access_token,
            refresh_token=session.refresh_token,
            token_expires_at=session.token_expires_at,
            madad_user_id=session.user_or_lead_ref,
        )

    # -- Step 2: consent + CR upload -----------------------------------------

    async def _business_email_send(
        self, state: OnboardingState, ctx: WorkflowContext
    ) -> dict[str, Any]:
        """Right after the lead says YES and the account is created, ask for
        their BUSINESS email. Capturing it makes the lead a normal,
        portal-loginable user and lets us detect a duplicate business before
        the CR step."""
        await self._send(ctx, state, "onboarding.business_email.ask")
        return self._step("business_email_send", ctx, business_email_status=None)

    async def _business_email_await(
        self, state: OnboardingState, ctx: WorkflowContext
    ) -> dict[str, Any]:
        reply = await_input({"waiting_for": "email", "step": "business_email"})
        email = _extract_email(reply_text(reply))
        if not email:
            # Off-script / not an email — clarify in context and keep waiting.
            await self._smart_contextual(
                ctx, state, reply,
                "Please reply with your business email (e.g. name@company.com) "
                "so we can set up your account. 📧",
            )
            return self._step("business_email_await", ctx, business_email_status="await")
        try:
            result = await self._identity.set_business_email(
                email=email,
                user_id=state.madad_user_id,
                channel=_channel(ctx),
                identifier=ctx.identity,
            )
        except Exception as exc:  # noqa: BLE001 — transport error -> let them retry
            ctx.logger.warning("business_email.set_failed", error=str(exc)[:200])
            return self._step("business_email_await", ctx, business_email_status="await")
        if result.get("conflict"):
            return self._step(
                "business_email_await", ctx,
                business_email=email, business_email_status="conflict",
            )
        # ok (or the unlikely alreadyPortalUser) — proceed; record step 2.
        await self._update_progress(state, ctx, step=2)
        return self._step(
            "business_email_await", ctx,
            business_email=email, business_email_status="proceed",
        )

    def _route_business_email(self, state: OnboardingState) -> str:
        status = (state.business_email_status or "").lower()
        if status == "proceed":
            return "proceed"
        if status == "conflict":
            return "conflict"
        return "await_again"

    async def _business_email_conflict_send(
        self, state: OnboardingState, ctx: WorkflowContext
    ) -> dict[str, Any]:
        """The email is already registered to another account — ask for a
        different business email, or to contact support."""
        await self._send(ctx, state, "onboarding.business_email.conflict")
        return self._step("business_email_conflict_send", ctx, business_email_status=None)

    async def _consent_send(
        self, state: OnboardingState, ctx: WorkflowContext
    ) -> dict[str, Any]:
        await self._send(ctx, state, "onboarding.consent.request")
        return self._step("consent_send", ctx)

    async def _consent_await(
        self, state: OnboardingState, ctx: WorkflowContext
    ) -> dict[str, Any]:
        reply = await_input({"waiting_for": "upload", "step": "consent_cr"})
        help_template = _off_script_template(reply)
        if help_template is not None:
            await self._send(
                ctx,
                state,
                "onboarding.help.contextual",
                {"answer": self._answer_for(help_template), "next_step": _next_step_hint(state)},
            )
            return self._step("consent_await", ctx, consent=False)
        if _is_status_query(reply) or _is_portal_query(reply):
            await self._send(
                ctx,
                state,
                "onboarding.help.contextual",
                {
                    "answer": "Your application is still in progress. We still need your CR before submitting it.",  # noqa: E501
                    "next_step": _next_step_hint(state),
                },
            )
            return self._step("consent_await", ctx, consent=False)
        if _is_casual_message(reply):
            await self._send(
                ctx,
                state,
                "onboarding.help.contextual",
                {"answer": "I’m here and ready to help.", "next_step": _next_step_hint(state)},
            )
            return self._step("consent_await", ctx, consent=False)
        attachments = _valid_upload_attachments(reply)
        if not attachments:
            # No document — it's a question or chit-chat. Answer it in context
            # (Groq), falling back to the upload nudge. (user 2026-06-14)
            await self._contextual_off_script(
                ctx, state, reply,
                default_answer="Whenever you're ready, please share your Commercial "
                "Registration (CR) as a PDF or a clear photo. 🙂",
            )
            return self._step("consent_await", ctx, consent=False)
        first = attachments[0]
        # Immediate ack so the user always sees a response — even if the
        # downstream cr_upload_base64 / financials_send chain hiccups (Bug #1).
        try:
            await self._send(ctx, state, "onboarding.cr.received")
        except Exception as exc:  # noqa: BLE001 — ack failure must not kill the run
            ctx.logger.warning("cr_received_ack.failed", error=str(exc)[:200])
        return self._step(
            "consent_await",
            ctx,
            consent=True,
            cr_ref=first.get("filename"),
            cr_filename=first.get("filename"),
            cr_content_base64=first.get("content_base64") or "",
            cr_mime_type=first.get("mime_type"),
        )

    async def _cr_upload_base64(
        self, state: OnboardingState, ctx: WorkflowContext
    ) -> dict[str, Any]:
        token, refresh, expires = await self._live_token(state, ctx)
        # Classify-and-upload (not the forced CR upload) so we learn what the
        # doc ACTUALLY is. A real CR classifies as commercial_registration and
        # lands in the CR slot exactly as before; a random doc classifies as
        # something else and we suppress the "registered in Qatar — all good"
        # affirmation downstream (user 2026-06-13: demo can't claim Qatar
        # registration when the SME uploaded a non-CR). Default stays True so a
        # classifier miss never drops the line on a genuine CR.
        cr_verified = state.cr_verified
        if token and state.cr_ref:
            try:
                resp = await self._kyc.classify_and_upload_document_base64(
                    access_token=token,
                    content_base64=state.cr_content_base64 or "",
                    filename=state.cr_ref,
                    mime_type=state.cr_mime_type,
                )
                detected: str | None = None
                if isinstance(resp, dict):
                    raw = (
                        resp.get("document_type")
                        or resp.get("documentType")
                        or resp.get("resolved_document_type")
                    )
                    if isinstance(raw, str) and raw:
                        detected = _workflow_doc_type(raw)
                # Only flip the verdict when the classifier confidently named a
                # type: a CONFIRMED non-CR (and not the catch-all
                # "additional_document") → not a CR. Unknown / no type → keep the
                # default so a real CR is never wrongly downgraded.
                if detected and detected != "additional_document":
                    cr_verified = detected == "commercial_registration"
            except Exception as exc:  # noqa: BLE001 — degrade in staging
                ctx.logger.warning(
                    "cr_upload.failed", error=str(exc)[:200],
                    note="staging-tolerant: continuing without CR uploaded",
                )
        # Step 2 — CR uploaded (backend journey_status: INCOMPLETE).
        progress_step = await self._update_progress(state, ctx, step=2)
        return self._step(
            "cr_upload_base64", ctx,
            cr_verified=cr_verified,
            access_token=token, refresh_token=refresh, token_expires_at=expires,
            onboarding_progress_step=progress_step or state.onboarding_progress_step,
        )

    # -- Step 3: eligibility intake ------------------------------------------

    async def _eligibility_intake_send(
        self, state: OnboardingState, ctx: WorkflowContext
    ) -> dict[str, Any]:
        await self._send(ctx, state, "onboarding.eligibility.intake.request")
        await self._reminders.schedule(
            "eligibility_pending",
            channel=_channel(ctx),
            identity=ctx.identity,
            target_ref=state.madad_user_id or ctx.session_id,
        )
        return self._step("eligibility_intake_send", ctx)

    async def _eligibility_intake_await(
        self, state: OnboardingState, ctx: WorkflowContext
    ) -> dict[str, Any]:
        reply = await_input({"waiting_for": "eligibility_form", "step": "eligibility"})
        help_template = _off_script_template(reply)
        if help_template is not None:
            await self._send(
                ctx,
                state,
                "onboarding.help.contextual",
                {"answer": self._answer_for(help_template), "next_step": _next_step_hint(state)},
            )
            return self._step("eligibility_intake_await", ctx, eligibility_form_data={})
        if _is_status_query(reply) or _is_portal_query(reply):
            await self._send(
                ctx,
                state,
                "onboarding.help.contextual",
                {
                    "answer": "Your application is still in progress. We have not submitted it yet.",  # noqa: E501
                    "next_step": _next_step_hint(state),
                },
            )
            return self._step("eligibility_intake_await", ctx, eligibility_form_data={})
        if _is_casual_message(reply) or _is_short_negative(reply):
            await self._send(
                ctx,
                state,
                "onboarding.help.contextual",
                {
                    "answer": "No worries — I will not submit that as your eligibility details.",
                    "next_step": _next_step_hint(state),
                },
            )
            return self._step("eligibility_intake_await", ctx, eligibility_form_data={})

        form = reply if isinstance(reply, dict) else {}
        form_data = {
            k: v for k, v in form.items() if k not in {"type", "text", "attachments"}
        }
        if not form_data:
            form_data = _parse_eligibility_text(reply_text(reply))
        if not form_data:
            # Couldn't read it as the eligibility details — answer it as an
            # off-script question/chat (Groq) instead of re-sending the form
            # prompt verbatim. (user 2026-06-14)
            await self._contextual_off_script(
                ctx, state, reply,
                default_answer="When you're ready, please share the quick business "
                "details we asked for so we can check your eligibility. 🙂",
            )
        await self._reminders.suppress(target_ref=state.madad_user_id or ctx.session_id)
        return self._step(
            "eligibility_intake_await", ctx, eligibility_form_data=form_data
        )

    async def _eligibility_update(
        self, state: OnboardingState, ctx: WorkflowContext
    ) -> dict[str, Any]:
        eligible = True
        normalized: dict[str, Any] = {}
        token, refresh, expires = await self._live_token(state, ctx)
        if token:
            # Merge operator-supplied form data on top of demo defaults so
            # the seven required UAT fields are always present. The operator
            # only needs to override the fields they want to change.
            payload = {**DEFAULT_ELIGIBILITY_FORM, **state.eligibility_form_data}
            try:
                result = await self._kyc.update_eligibility(
                    access_token=token, data=payload
                )
                if isinstance(result, dict):
                    # The real cluster returns ``journeyStatus`` (not
                    # ``eligible``) on success — derive eligibility from
                    # the canonical 16 statuses. Fall back to the legacy
                    # ``eligible`` field for InMemoryKycClient + any older
                    # response shapes.
                    new_status = result.get("journeyStatus")
                    if isinstance(new_status, str):
                        eligible = new_status != "IN_ELIGIBLE"
                    elif "eligible" in result:
                        eligible = bool(result["eligible"])
                # Read the backend's normalized values back into state so the
                # rest of the workflow uses canonical enums instead of the
                # raw form values we sent.
                bd = await self._pay.get_business_details(access_token=token)
                if isinstance(bd, dict):
                    normalized = {
                        "business_age": bd.get("businessAge"),
                        "cr_validity": bd.get("crValidity"),
                        "company_type": bd.get("companyType"),
                        "sector": bd.get("sector"),
                        "turnover": bd.get("turnover"),
                        "employees": bd.get("employees"),
                        "is_qatar_based": bd.get("isQatarBased"),
                    }
            except Exception as exc:  # noqa: BLE001 — degrade in staging
                ctx.logger.warning(
                    "eligibility.update_failed",
                    error=str(exc)[:200],
                    note="staging-tolerant: continuing with eligible=True",
                )
        merged_form = {**state.eligibility_form_data, **{
            k: v for k, v in normalized.items() if v is not None
        }}
        return self._step(
            "eligibility_update", ctx,
            eligible=eligible,
            eligibility_form_data=merged_form,
            access_token=token, refresh_token=refresh, token_expires_at=expires,
        )

    async def _not_eligible(
        self, state: OnboardingState, ctx: WorkflowContext
    ) -> dict[str, Any]:
        await self._send(ctx, state, "onboarding.not_eligible")
        return self._step("not_eligible", ctx, outcome="not_eligible")

    # -- Step 4: financials ---------------------------------------------------

    async def _financials_send(
        self, state: OnboardingState, ctx: WorkflowContext
    ) -> dict[str, Any]:
        # Bug #1 (2026-06-09): the financials prompt is the next user-facing
        # message after CR upload. A transient messenger / reminder failure
        # used to kill the run silently — guard so we always progress to
        # financials_await and let the SME's next reply re-trigger the prompt.
        # Only assert "registered in Qatar — all good" when the CR step's upload
        # actually classified as a Commercial Registration (user 2026-06-13).
        cr_affirmation = (
            "We can see that your business is registered in Qatar — all good "
            "so far! ✅\n\n"
            if state.cr_verified
            else ""
        )
        try:
            await self._send(
                ctx, state, "onboarding.financials.request",
                {"cr_affirmation": cr_affirmation},
            )
        except Exception as exc:  # noqa: BLE001 — degrade gracefully
            ctx.logger.warning("financials_request.send_failed", error=str(exc)[:200])
        try:
            await self._reminders.schedule(
                "financials_pending",
                channel=_channel(ctx),
                identity=ctx.identity,
                target_ref=state.madad_user_id or ctx.session_id,
            )
        except Exception as exc:  # noqa: BLE001 — nudges are non-critical
            ctx.logger.warning("financials_pending.schedule_failed", error=str(exc)[:200])
        return self._step("financials_send", ctx)

    async def _financials_await(
        self, state: OnboardingState, ctx: WorkflowContext
    ) -> dict[str, Any]:
        reply = await_input({"waiting_for": "upload", "step": "financials"})
        help_template = _off_script_template(reply)
        if help_template is not None:
            await self._send(
                ctx,
                state,
                "onboarding.help.contextual",
                {"answer": self._answer_for(help_template), "next_step": _next_step_hint(state)},
            )
            return self._step("financials_await", ctx, financials_received=False)
        if _is_status_query(reply) or _is_portal_query(reply):
            await self._send(
                ctx,
                state,
                "onboarding.help.contextual",
                {
                    "answer": "Your application is still in progress. We still need your audited financial statement before submitting it.",  # noqa: E501
                    "next_step": _next_step_hint(state),
                },
            )
            return self._step("financials_await", ctx, financials_received=False)
        if _is_casual_message(reply):
            await self._send(
                ctx,
                state,
                "onboarding.help.contextual",
                {"answer": "I’m here and ready to help.", "next_step": _next_step_hint(state)},
            )
            return self._step("financials_await", ctx, financials_received=False)
        attachments = _valid_upload_attachments(reply)
        await self._reminders.suppress(target_ref=state.madad_user_id or ctx.session_id)
        if not attachments:
            # No document — answer the question/chat in context (Groq), falling
            # back to the upload nudge. (user 2026-06-14)
            await self._contextual_off_script(
                ctx, state, reply,
                default_answer="Whenever you're ready, please share your latest "
                "Audited Financial Statement as a PDF or a clear photo. 🙂",
            )
            return self._step("financials_await", ctx, financials_received=False)
        first = attachments[0]
        return self._step(
            "financials_await",
            ctx,
            financials_received=True,
            financials_content_base64=first.get("content_base64") or "",
            financials_filename=first.get("filename") or "",
            financials_mime_type=first.get("mime_type"),
        )

    async def _financials_upload_base64(
        self, state: OnboardingState, ctx: WorkflowContext
    ) -> dict[str, Any]:
        if state.access_token and state.financials_received:
            try:
                await self._kyc.upload_audited_financial_report(
                    access_token=state.access_token,
                    content_base64=state.financials_content_base64 or "",
                    filename=state.financials_filename or "audited_report.pdf",
                    mime_type=state.financials_mime_type,
                )
            except Exception as exc:  # noqa: BLE001 — degrade in staging
                ctx.logger.warning(
                    "financials_upload.failed", error=str(exc)[:200],
                    note="staging-tolerant: continuing without financials uploaded",
                )
        # Spec Step 3: right after the audited report, confirm the account is
        # created and share the Madad reference number, before pre-qualification.
        ref = ""
        if state.access_token:
            try:
                info = await self._identity.me(access_token=state.access_token)
                if isinstance(info, dict):
                    nested = info.get("user")
                    user = nested if isinstance(nested, dict) else info
                    ref = str(user.get("uniqueId") or user.get("unique_id") or "")
            except Exception:  # noqa: BLE001
                pass
        if not ref:
            ref = (re.sub(r"\D", "", ctx.identity or "")[-8:] or "MADAD")
        await self._send(ctx, state, "onboarding.account.created", {"ref": ref})
        # Step 3 — CRITICAL GATE. Per Ishan (2026-06-07): backend hard-gates
        # the pre-qualified document checklist on step >= 3. Without this call
        # the prequalification.completed webhook either won't fire or won't
        # carry the document checklist payload.
        progress_step = await self._update_progress(state, ctx, step=3)
        return self._step(
            "financials_upload_base64",
            ctx,
            onboarding_progress_step=progress_step or state.onboarding_progress_step,
        )

    # -- Step 5-6: admin-requested documents + counterparties ----------------

    async def _documents_list_fetch(
        self, state: OnboardingState, ctx: WorkflowContext
    ) -> dict[str, Any]:
        # WhatsApp onboarding always shows the FULL Madad checklist. The backend's
        # admin-requested list is only a subset (it omits docs the admin hasn't
        # explicitly requested yet), which made the agent show just 3 items.
        if ctx.channel is Channel.WHATSAPP:
            return self._step(
                "documents_list_fetch",
                ctx,
                missing_documents=list(DEFAULT_WHATSAPP_REQUIRED_DOCS),
            )
        missing: list[str] = []
        if state.access_token:
            result = await self._kyc.get_admin_requested_documents(
                access_token=state.access_token
            )
            if isinstance(result, dict):
                missing = list(result.get("missing", []))
        return self._step("documents_list_fetch", ctx, missing_documents=missing)

    async def _buyers_collect_send(
        self, state: OnboardingState, ctx: WorkflowContext
    ) -> dict[str, Any]:
        await self._send(ctx, state, "onboarding.buyers.request")
        return self._step("buyers_collect_send", ctx)

    async def _buyers_collect_await(
        self, state: OnboardingState, ctx: WorkflowContext
    ) -> dict[str, Any]:
        reply = await_input({"waiting_for": "buyers", "step": "buyers"})
        help_template = _off_script_template(reply)
        if help_template is not None:
            await self._send(
                ctx,
                state,
                "onboarding.help.contextual",
                {"answer": self._answer_for(help_template), "next_step": _next_step_hint(state)},
            )
            return self._step("buyers_collect_await", ctx, buyers=list(state.buyers))
        if _is_status_query(reply) or _is_portal_query(reply):
            await self._send(
                ctx,
                state,
                "onboarding.help.contextual",
                {
                    "answer": "Your application is still in progress. We still need your main buyer details before submitting it.",  # noqa: E501
                    "next_step": _next_step_hint(state),
                },
            )
            return self._step("buyers_collect_await", ctx, buyers=list(state.buyers))
        if _is_casual_message(reply) or _is_short_negative(reply):
            await self._send(
                ctx,
                state,
                "onboarding.help.contextual",
                {
                    "answer": "No problem — I will not submit that as buyer details.",
                    "next_step": _next_step_hint(state),
                },
            )
            return self._step("buyers_collect_await", ctx, buyers=list(state.buyers))
        buyer = reply if isinstance(reply, dict) else {}
        if not any(k in buyer for k in {"name", "cr_number", "contact_person", "contact_number", "contact_email", "buyer_type", "buyer_sector"}):  # noqa: E501
            parsed = _parse_buyer_text(reply_text(reply))
            if parsed:
                buyer = parsed
        # Only forward fields the UAT add_buyer tool accepts; everything else
        # (e.g. demo runner's `country`) is dropped so the call passes
        # pydantic validation at the cluster.
        allowed = {
            "name", "cr_number", "contact_person", "contact_number",
            "contact_email", "buyer_type", "buyer_sector",
        }
        data = {k: v for k, v in buyer.items() if k in allowed}
        if not data:
            # Not buyer details — answer the off-script question/chat (Groq),
            # falling back to the buyer-details nudge. (user 2026-06-14)
            await self._contextual_off_script(
                ctx, state, reply,
                default_answer="When you're ready, please share your main buyer's "
                "details (name, country, and contact). 🙂",
            )
            return self._step("buyers_collect_await", ctx, buyers=list(state.buyers))
        if data and state.access_token:
            try:
                await self._kyc.add_buyer(access_token=state.access_token, data=data)
            except Exception as exc:  # noqa: BLE001 — degrade in staging
                if _is_conflict_error(exc):
                    ctx.logger.info(
                        "add_buyer.already_exists",
                        note="backend 409: buyer already registered for this SME",
                    )
                else:
                    ctx.logger.warning(
                        "add_buyer.failed", error=str(exc)[:200],
                        note="staging-tolerant: continuing without buyer added",
                    )
        buyers = [*state.buyers, data] if data else list(state.buyers)
        return self._step("buyers_collect_await", ctx, buyers=buyers)

    async def _shareholders_collect_send(
        self, state: OnboardingState, ctx: WorkflowContext
    ) -> dict[str, Any]:
        await self._send(ctx, state, "onboarding.shareholders.request")
        return self._step("shareholders_collect_send", ctx)

    async def _shareholders_collect_await(
        self, state: OnboardingState, ctx: WorkflowContext
    ) -> dict[str, Any]:
        reply = await_input({"waiting_for": "shareholders", "step": "shareholders"})
        help_template = _off_script_template(reply)
        if help_template is not None:
            await self._send(
                ctx,
                state,
                "onboarding.help.contextual",
                {"answer": self._answer_for(help_template), "next_step": _next_step_hint(state)},
            )
            return self._step(
                "shareholders_collect_await", ctx, shareholders=list(state.shareholders)
            )
        if _is_status_query(reply) or _is_portal_query(reply):
            await self._send(
                ctx,
                state,
                "onboarding.help.contextual",
                {
                    "answer": "Your application is still in progress. We still need shareholder details before submitting it.",  # noqa: E501
                    "next_step": _next_step_hint(state),
                },
            )
            return self._step(
                "shareholders_collect_await", ctx, shareholders=list(state.shareholders)
            )
        if _is_casual_message(reply) or _is_short_negative(reply):
            await self._send(
                ctx,
                state,
                "onboarding.help.contextual",
                {
                    "answer": "No problem — I will not submit that as shareholder details.",
                    "next_step": _next_step_hint(state),
                },
            )
            return self._step(
                "shareholders_collect_await", ctx, shareholders=list(state.shareholders)
            )
        payload = reply if isinstance(reply, dict) else {}
        raw = payload.get("shareholders")
        items: list[dict[str, Any]] = list(raw) if isinstance(raw, list) else []
        if not items:
            items = _parse_shareholder_text(reply_text(reply), ctx.identity)
        # Per Ishan's KYC_ADD_SHAREHOLDERS schema (2026-06-06): each
        # shareholder requires ``name + phoneNumber``; the cluster also
        # accepts ``firstName / lastName / middleName / email / address``
        # as optional. Anything else (percentage / nationality / document
        # fields) is rejected with HTTP 400 — those go through the
        # separate shareholder-documents upload + KYC tools.
        allowed = {
            "name", "phoneNumber",
            "firstName", "lastName", "middleName", "email", "address",
        }
        sanitized: list[dict[str, Any]] = []
        for item in items:
            if not isinstance(item, dict):
                continue
            record = {k: v for k, v in item.items() if k in allowed and v is not None}
            if "name" not in record or "phoneNumber" not in record:
                # Fall back to deriving name from firstName/lastName when the
                # demo runner supplies them split.
                if "firstName" in record and "lastName" in record and "name" not in record:
                    record["name"] = f"{record['firstName']} {record['lastName']}".strip()
                if "name" not in record or "phoneNumber" not in record:
                    ctx.logger.warning(
                        "add_shareholders.invalid_record",
                        keys_supplied=sorted(item.keys()),
                        note="missing required name / phoneNumber — dropped",
                    )
                    continue
            sanitized.append(record)
        if not sanitized:
            # Not shareholder details — answer the off-script question/chat
            # (Groq), falling back to the shareholder-details nudge.
            await self._contextual_off_script(
                ctx, state, reply,
                default_answer="When you're ready, please share your shareholders' "
                "details (name and percentage). 🙂",
            )
            return self._step(
                "shareholders_collect_await", ctx, shareholders=list(state.shareholders)
            )
        if sanitized and state.access_token:
            try:
                await self._kyc.add_shareholders(
                    access_token=state.access_token, shareholders=sanitized
                )
            except Exception as exc:  # noqa: BLE001 — degrade in staging
                ctx.logger.warning(
                    "add_shareholders.failed",
                    error=str(exc)[:300],
                    attempted_count=len(sanitized),
                    note="staging-tolerant: workflow continues",
                )
        return self._step(
            "shareholders_collect_await", ctx, shareholders=sanitized
        )

    async def _documents_upload_loop_send(
        self, state: OnboardingState, ctx: WorkflowContext
    ) -> dict[str, Any]:
        # First entry uses the checklist template; re-entries (missing-docs
        # loop-back) use the shorter "still missing" template.
        already_asked = any(
            h.step == "documents_upload_loop_send" for h in state.history
        )
        template_key = (
            "onboarding.documents.missing"
            if already_asked
            else "onboarding.documents.checklist"
        )
        # Progress meter: count uploaded vs the full required set so re-entries
        # show "N of M received" instead of repeating the same "still needed" text.
        total = len(DEFAULT_WHATSAPP_REQUIRED_DOCS)
        received = max(0, total - len(state.missing_documents))
        await self._send(
            ctx,
            state,
            template_key,
            {
                "documents": _format_documents(state.missing_documents),
                "received": received,
                "total": total,
            },
        )
        # A9: nudge.incomplete_docs.{1,2,3} bodies reference {{ documents }}
        # so the missing-list shows up in each scheduled reminder. Reuse the
        # already-formatted list rather than re-rendering at dispatch time.
        await self._reminders.schedule(
            "incomplete_docs",
            channel=_channel(ctx),
            identity=ctx.identity,
            target_ref=state.madad_user_id or ctx.session_id,
            variables={
                "documents": _format_documents(state.missing_documents),
            },
        )
        return self._step("documents_upload_loop_send", ctx)

    async def _documents_upload_loop_await(
        self, state: OnboardingState, ctx: WorkflowContext
    ) -> dict[str, Any]:
        reply = await_input({"waiting_for": "upload", "step": "documents"})
        # A backend webhook / status event (e.g. admin pre-qualified the user)
        # can land on this step if they haven't uploaded yet. Do NOT re-send the
        # document checklist — treat the document phase as satisfied and let the
        # flow advance to the status/payment branch, carrying the status hint.
        forced_status = _extract_journey_status(reply)
        if forced_status is not None:
            await self._reminders.suppress(
                target_ref=state.madad_user_id or ctx.session_id
            )
            # Bug #12 (UAT 2026-06-09, Ishan diagnosis): when QUALIFIED+
            # arrives mid-docs-loop, the SAME event must fast-forward
            # through ``payment_wait_await`` too — backend only fires
            # ``madad_score.ready`` once, so requiring a second trigger
            # left the run stuck at payment_wait until the admin re-fired.
            # Capture payment_ready + madad_score on the same resume.
            fast_forward = forced_status in {
                JourneyStatus.QUALIFIED,
                JourneyStatus.ACCEPTED,
                JourneyStatus.OFFER_ACCEPTED,
                JourneyStatus.ACTIVATED,
            }
            score = _extract_madad_score(reply)
            fields: dict[str, Any] = {
                "missing_documents": list(state.missing_documents),
                "documents_received": True,
                "journey_status": forced_status,
                "last_status_source": _extract_status_source(reply),
            }
            if fast_forward:
                fields["payment_ready"] = True
                if score is not None:
                    fields["madad_score"] = score
                # Refinement (2026-06-09): when admin overrides the
                # checklist, the natural ``documents_complete`` node is
                # skipped — so its progress=5 marker would never fire on
                # this path. Fire it here so the backend gets the full
                # ordered sequence regardless of route.
                progress_step = await self._update_progress(state, ctx, step=5)
                if progress_step is not None:
                    fields["onboarding_progress_step"] = progress_step
            return self._step("documents_upload_loop_await", ctx, **fields)
        # End-of-upload settle (UAT 2026-06-13): the status-poller sweep resumes
        # this run with a synthetic ``docs_settle`` event once the SME has stopped
        # uploading for the quiet window. THIS is where the checklist + the
        # tappable YES/NO button prompt are sent — from the workflow, so the
        # buttons are interactive — exactly once per quiet period, at the very
        # end of the batch (never mid-upload). Re-armed by each new upload wave
        # (which clears docs_settle_prompted).
        if _is_docs_settle(reply):
            if state.missing_documents and not state.docs_settle_prompted:
                try:
                    await self._send_pending_docs(
                        ctx, state, list(state.missing_documents)
                    )
                    await self._send_more_docs_prompt(ctx, state)
                except Exception as exc:  # noqa: BLE001
                    ctx.logger.warning(
                        "docs_settle_prompt.failed", error=str(exc)[:200]
                    )
            return self._step(
                "documents_upload_loop_await",
                ctx,
                docs_settle_prompted=True,
                more_docs_prompt_at=ctx.clock.now().isoformat(),
                missing_documents=list(state.missing_documents),
                documents_received=False,
            )
        attachments = _valid_upload_attachments(reply)
        if not attachments:
            # Bug #16 (UAT 2026-06-09): per spec page 8 "PENDING DOCS",
            # the SME can ask "what am I still missing?" anytime and the
            # agent answers with the running list. With Bug #16's brief-
            # receipt design (no proactive checklist body during uploads)
            # this self-service path is the SME's only on-demand way to
            # see what's left — handle the intent before falling through
            # to the generic _smart_contextual reply.
            if _is_pending_docs_query(reply):
                await self._send_pending_docs(
                    ctx, state, list(state.missing_documents)
                )
                return self._step(
                    "documents_upload_loop_await",
                    ctx,
                    missing_documents=list(state.missing_documents),
                    documents_received=False,
                )
            # "NO / done" → the SME wants to proceed even though some required
            # docs weren't detected (frustrated-user escape hatch, user
            # 2026-06-12). Only honoured once we've actually shown the "any
            # more documents?" prompt, so a stray "no" earlier in the phase
            # can't skip the document step.
            if is_no(reply) and (state.more_docs_prompt_at or state.docs_uploaded_count > 0):
                # Per user (2026-06-12): on NO, send the coffee / "all received,
                # we'll review within 24h" message (once) so the SME gets a
                # clear next-step confirmation — not a bare "moving on".
                if not state.documents_complete_sent:
                    await self._send(ctx, state, "onboarding.documents.complete")
                return self._step(
                    "documents_upload_loop_await", ctx,
                    docs_proceed=True,
                    documents_complete_sent=True,
                    missing_documents=list(state.missing_documents),
                    documents_received=False,
                )
            # "YES" → they have more to send; acknowledge and stay parked.
            if is_yes(reply) and (state.more_docs_prompt_at or state.docs_uploaded_count > 0):
                await self._send(
                    ctx, state, "onboarding.help.contextual",
                    {"answer": "Sure — send the rest whenever you're ready, "
                     "as a PDF or photo. 📎", "next_step": ""},
                )
                return self._step(
                    "documents_upload_loop_await", ctx,
                    missing_documents=list(state.missing_documents),
                    documents_received=False,
                )
            # No file — it's a question or chit-chat. Answer it in
            # context (the agent must actually understand, not robotically nag
            # "text alone is not enough"), then stay parked for the upload.
            fallback = (
                "No problem 🙂 Whenever you have them, just send any of the "
                "documents here as a PDF or photo and our team will take it "
                "from there."
            )
            await self._smart_contextual(ctx, state, reply, fallback)
            return self._step(
                "documents_upload_loop_await",
                ctx,
                missing_documents=list(state.missing_documents),
                documents_received=False,
            )
        # Bug #1b (2026-06-09): immediate ack the instant valid attachments
        # land — large ZIPs can keep the classify+upload chain busy for
        # tens of seconds; mirrors the CR ack so the SME never sits in
        # silence. The ack itself is wrapped — its failure must not regress
        # the upload pipeline below.
        #
        # Bug #11 (UAT 2026-06-09): debounce — Madad's bridge POSTs each
        # ZIP-member as a SEPARATE inbound (8+ messages in a few seconds
        # for a typical doc batch). Without the debounce the SME saw the
        # ack 8 times in a row. Re-fire only if the previous ack is older
        # than DOCS_PROCESSING_ACK_TTL_SECONDS.
        now = ctx.clock.now()
        prior_ack = _parse_iso_or_none(state.documents_processing_ack_at)
        ack_age = (now - prior_ack).total_seconds() if prior_ack else None
        processing_ack_at: str | None = state.documents_processing_ack_at
        if ack_age is None or ack_age >= DOCS_PROCESSING_ACK_TTL_SECONDS:
            try:
                await self._send(ctx, state, "onboarding.documents.processing")
                processing_ack_at = now.isoformat()
            except Exception as exc:  # noqa: BLE001
                ctx.logger.warning(
                    "documents_processing_ack.failed", error=str(exc)[:200]
                )
        # A7 (Ishan 2026-06-07): ZIP attachments now route through the
        # backend's ``classify_and_upload_zip_base64`` tool. The backend
        # unzips server-side, classifies every member, and returns the
        # per-file checklist (file_name, resolved document_type,
        # confidently_classified). This is one round-trip instead of N
        # (one per member) and matches the msme-portal pipeline exactly.
        # The previous local-unzipping path stays as a fallback for when
        # the backend tool errors.
        pending: list[str] = list(state.missing_documents)
        # Bug #10b (2026-06-09): snapshot the set we ASKED the SME for so a
        # classifier mis-label (e.g. backend tagged a passport as CR — Madad
        # QA screenshot 2026-06-09) cannot legitimize an unrelated upload
        # as a ✅ validated entry. Only docs that were on this batch's
        # pending list earn the ✅; anything else lands as ⏳ "received,
        # team will review" — honest, never a false validation.
        #
        # Optional shareholder docs (2026-06-10): include them in the
        # ``expected`` set so an upload still earns ✅ when classified —
        # but they're NOT in ``pending``, so they never count toward
        # remaining-required and the loop can naturally exhaust without
        # them. See [[project_optional_docs]].
        expected: set[str] = set(pending) | set(DEFAULT_WHATSAPP_OPTIONAL_DOCS)
        validated: list[str] = []  # uploaded + classified into expected → ✅
        unprocessed: list[str] = []  # uploaded but off-checklist / failed → ⏳
        saw_zip = False

        # Mint a live backend token from the verified WhatsApp identity. A run
        # parked since an earlier turn can resume after the 900s token TTL with
        # an empty/expired ``state.access_token``; gating the classify-and-upload
        # calls below on that raw value silently dropped EVERY upload (no token →
        # skipped → docs marked "received" but never sent). The WhatsApp identity
        # is already verified (Meta-signed webhook + WA-verified number), so we
        # always mint a fresh token on demand here — no document may be lost.
        token, refresh, expires = await self._live_token(state, ctx)

        # Pass 1 — process ZIPs server-side. Anything that isn't a ZIP or
        # whose server-side classify fails falls through to pass 2.
        non_zip: list[dict[str, Any]] = []
        for att in attachments:
            if not _is_zip_attachment(att):
                non_zip.append(att)
                continue
            if not token:
                non_zip.append(att)
                continue
            try:
                # Hard wall-clock cap so a hung Cloud-Run ZIP processor
                # can't trap the node behind the workflow runtime's own
                # 60s-per-attempt budget (Bug #1b 2026-06-09 forensic:
                # one ZIP call hung 3 minutes before the run timed out
                # silently). 25s is enough for normal traffic and falls
                # back to the local-unzip + per-file path on overrun.
                zip_response = await asyncio.wait_for(
                    self._kyc.classify_and_upload_zip_base64(
                        access_token=token,
                        content_base64=att.get("content_base64") or "",
                        filename=att.get("filename") or "",
                    ),
                    timeout=25.0,
                )
            except Exception as exc:  # noqa: BLE001 — fall back to local unzip
                ctx.logger.warning(
                    "classify_and_upload_zip.failed",
                    error=str(exc)[:200],
                    note="falling back to local unzip + per-file classify",
                )
                expanded, _ = _expand_zip_attachments([att])
                non_zip.extend(expanded)
                continue
            saw_zip = True
            # Per-file checklist shape per Ishan's docstring:
            # ``[{file_name, document_type, confidently_classified}, ...]``.
            # camelCase + snake_case + body-envelope all tolerated.
            checklist: list[Any] = []
            if isinstance(zip_response, dict):
                body = zip_response.get("body")
                inner = body if isinstance(body, dict) else zip_response
                raw = inner.get("checklist") or inner.get("files") or []
                if isinstance(raw, list):
                    checklist = raw
            for entry in checklist:
                if not isinstance(entry, dict):
                    continue
                backend_type = (
                    entry.get("document_type")
                    or entry.get("documentType")
                    or entry.get("resolved_document_type")
                )
                if not isinstance(backend_type, str) or not backend_type:
                    continue
                zip_doc_type = _workflow_doc_type(backend_type)
                # Bug #10b: only docs that match the asked-for list earn ✅;
                # otherwise show ⏳ so a classifier mislabel never claims a
                # checklist slot the SME hasn't actually filled.
                if zip_doc_type in expected:
                    if zip_doc_type in pending:
                        pending.remove(zip_doc_type)
                    if zip_doc_type not in validated:
                        validated.append(zip_doc_type)
                elif zip_doc_type not in unprocessed:
                    unprocessed.append(zip_doc_type)

        # Pass 2 — non-ZIP attachments (and any ZIP that fell back to local).
        # A5 (Ishan 2026-06-07): prefer the classify-and-upload tool over our
        # own filename-keyword inference. Bug #17 (UAT 2026-06-09): run all
        # files CONCURRENTLY — sequential per-file processing accumulated
        # to >60s when one file (AoA) timed out at the 25s cap, blowing
        # past the workflow runtime's step_timeout and triggering a full
        # node retry × 3 → RetryExhaustedError → run died with the rest of
        # the batch unprocessed. With asyncio.gather the wall-clock is
        # max(per-file) ≈ 25s regardless of how many files are in the batch.
        async def _classify_one(att: dict[str, Any]) -> tuple[bool, str | None]:
            if not token:
                return False, None
            filename = att.get("filename") or ""
            try:
                classify_response = await asyncio.wait_for(
                    self._kyc.classify_and_upload_document_base64(
                        access_token=token,
                        content_base64=att.get("content_base64") or "",
                        filename=filename,
                        mime_type=att.get("mime_type"),
                    ),
                    timeout=25.0,
                )
            except Exception as exc:  # noqa: BLE001 — degrade in staging
                ctx.logger.warning(
                    "classify_and_upload.failed",
                    filename=filename,
                    error=str(exc)[:200],
                    note="staging-tolerant: continuing without this doc",
                )
                return False, None
            resolved: str | None = None
            if isinstance(classify_response, dict):
                backend_type = (
                    classify_response.get("document_type")
                    or classify_response.get("documentType")
                    or classify_response.get("resolved_document_type")
                )
                if isinstance(backend_type, str):
                    resolved = _workflow_doc_type(backend_type)
            return True, resolved

        if non_zip:
            classify_results = await asyncio.gather(
                *[_classify_one(att) for att in non_zip]
            )
        else:
            classify_results = []

        for att, (uploaded_ok, resolved_doc_type) in zip(
            non_zip, classify_results, strict=False
        ):
            filename = att.get("filename") or ""
            # QA #3 refinement (2026-06-09): two failure modes from the
            # classifier need different handling:
            #
            #   * Classifier returned a SPECIFIC wrong type (e.g. backend
            #     tagged a passport upload as "commercial_registration"):
            #     resolved_doc_type is set, but it's not on the asked-for
            #     list. Land as ⏳ "received, team will review" — never
            #     ✅ a false validation.
            #
            #   * Classifier said "additional_document" / returned nothing:
            #     we genuinely don't know what it is. Per QA: "don't block
            #     the user — assign to a required slot, Madad's team can
            #     re-assign later." Fall back to filename inference, then
            #     to the next pending slot. The slot earns ✅ provisionally.
            doc_type: str | None = resolved_doc_type
            classifier_unknown = (
                not doc_type or doc_type == "additional_document"
            )
            if classifier_unknown:
                inferred = att.get("document_type") or _infer_doc_type(filename)
                if inferred:
                    doc_type = inferred
                elif pending:
                    # Last-resort hint per QA #3: take the next required
                    # slot so the SME isn't stuck. Madad-side review will
                    # re-bucket if needed.
                    doc_type = pending[0]
            if not doc_type:
                continue
            # Bug #10b: only docs on the asked-for list earn ✅. Backend
            # picked a wrong type? → land as ⏳ "received, team will
            # review". The SME sees an honest receipt, never a false
            # validation that fast-forwards the checklist.
            if uploaded_ok and doc_type in expected:
                if doc_type in pending:
                    pending.remove(doc_type)
                if doc_type not in validated:
                    validated.append(doc_type)
            elif doc_type not in unprocessed and doc_type not in validated:
                unprocessed.append(doc_type)
        # Acknowledge exactly what landed: ✅ per accepted document, ⏳ per
        # one we received but couldn't auto-validate (kept honest — no
        # false ✅). The cumulative ⚠️ checklist body is NO LONGER rendered
        # here (Bug #16 design): the spec only shows it on the first batch
        # / on demand, the coffee message marks "all done", and the user's
        # literal feedback was "one checklist at the end, not the start."
        # The pending-docs self-service query (handled in the no-attachments
        # branch above) covers the "what's missing?" case.
        # Receipt dedup (UAT 2026-06-13): a multi-file bulk arrives as many
        # overlapping inbound waves, and a doc validated in an earlier wave is no
        # longer in ``pending`` when a later wave re-encounters it → it would
        # land in ``unprocessed`` and get a contradictory "⏳ received" after its
        # "✅ validated" (and Audited Report got ⏳'d on every wave). Acknowledge
        # each doc type EXACTLY ONCE across the whole upload phase.
        already_acked = set(state.docs_acked)
        new_validated = [d for d in validated if d not in already_acked]
        new_unprocessed = [
            d for d in unprocessed if d not in already_acked and d not in new_validated
        ]
        docs_acked = list(
            dict.fromkeys([*state.docs_acked, *new_validated, *new_unprocessed])
        )
        await self._acknowledge_uploads(
            ctx, state, new_validated, new_unprocessed,
            saw_zip=saw_zip,
            missing_after=list(pending),
        )
        # Remaining = required docs this batch did not land. Tracked locally so
        # generic-filename uploads reliably complete the checklist (we do not
        # re-query the backend's requested-docs list, which kept returning the
        # just-uploaded docs as still-missing and caused the loop).
        missing = pending
        # Per Ishan + user (UAT 2026-06-10): classifier hangs (e.g. AoA)
        # leave required slots permanently "still needed" even when the
        # SME has uploaded enough total files. Track every attachment we
        # processed this turn (validated, ⏳, or even unclassifiable);
        # ``_route_documents`` exits when the cumulative count meets the
        # required count regardless of pending slots. Mirrors the doc-
        # service's count-based unblock (PR #4, commit 6c05b1c).
        new_uploaded_count = state.docs_uploaded_count + len(attachments)
        more_docs_prompt_at = state.more_docs_prompt_at
        last_upload_at = now.isoformat()
        if not missing:
            await self._reminders.suppress(
                target_ref=state.madad_user_id or ctx.session_id
            )
        # End-of-batch prompt, inline (user 2026-06-13): a ZIP is processed FULLY
        # in one server-side classify call, and a SINGLE isolated file is likewise
        # self-contained — in both cases the moment we reach here the upload is
        # done, so send the checklist + tappable YES/NO prompt ONCE, inline, right
        # now. Received-aware so uploaded-but-⏳ docs show as "received, under
        # review" instead of being re-listed as "still needed"; sets
        # docs_settle_prompted=True to suppress the settle sweep (no 45s wait, no
        # premature/duplicate checklist).
        #
        # A BULK burst arrives as many waves; we keep it on the sweep by treating
        # a one-attachment wave as "single" ONLY when isolated — the first upload
        # ever, or >90s after the previous one. Rapid bulk waves (<=90s apart)
        # fall through to the sweep so we don't fire a checklist per file (only a
        # bulk's very first wave can trip this — bounded to one extra checklist,
        # which the user accepted "for now").
        prior_upload = _parse_iso_or_none(state.docs_last_upload_at)
        upload_gap = (now - prior_upload).total_seconds() if prior_upload else None
        single_isolated = len(attachments) == 1 and (
            upload_gap is None or upload_gap > DOCS_SINGLE_INLINE_GAP_SECONDS
        )
        settle_now = bool(
            (saw_zip or single_isolated)
            and missing
            and not state.docs_settle_prompted
        )
        if settle_now:
            try:
                await self._send_pending_docs(
                    ctx, state, list(missing), received=docs_acked
                )
                await self._send_more_docs_prompt(ctx, state)
                more_docs_prompt_at = last_upload_at
            except Exception as exc:  # noqa: BLE001 — fall back to the sweep
                ctx.logger.warning("inline_settle_prompt.failed", error=str(exc)[:200])
                settle_now = False
        return self._step(
            "documents_upload_loop_await",
            ctx,
            missing_documents=missing,
            documents_received=bool(attachments),
            docs_uploaded_count=new_uploaded_count,
            access_token=token,
            refresh_token=refresh,
            token_expires_at=expires,
            documents_processing_ack_at=processing_ack_at,
            more_docs_prompt_at=more_docs_prompt_at,
            docs_last_upload_at=last_upload_at,
            docs_acked=docs_acked,
            docs_settle_prompted=settle_now,
        )

    async def _send_pending_docs(
        self,
        ctx: WorkflowContext,
        state: OnboardingState,
        missing: list[str],
        received: list[str] | None = None,
    ) -> None:
        """Reply to the SME's "what's still missing?" query with the
        running pending-docs list (spec page 8 PENDING DOCS self-service).

        Format mirrors the per-upload checklist body so it's familiar but
        scoped to the on-demand path — no batch receipts, just the
        current state.

        ``received`` are doc types the SME HAS uploaded but we couldn't
        auto-validate (landed as ⏳ "received, team will review"). They stay
        in ``missing`` (not confidently validated) but must NOT be re-listed
        as "still needed" — that told the SME to re-send a doc they'd already
        sent (user 2026-06-13, ZIP flow). Show them as "received, under
        review" instead so the checklist reflects what actually landed.
        """

        def _label(doc: str) -> str:
            return DOCUMENT_LABELS.get(doc, doc.replace("_", " ").title())

        received_set = set(received or [])
        all_required = list(DEFAULT_WHATSAPP_REQUIRED_DOCS)
        # Split the pending list: uploaded-but-unvalidated (⏳) vs never-sent.
        under_review = [d for d in missing if d in received_set]
        still_missing = [d for d in missing if d not in received_set]
        already_validated = [d for d in all_required if d not in missing]

        if not still_missing and not under_review:
            # SME asked but everything's already in — short, honest reply.
            await self._send(
                ctx, state, "onboarding.help.contextual",
                {
                    "answer": (
                        "🎉 All your documents are in! Our team is reviewing "
                        "them — we'll be in touch shortly."
                    ),
                    "next_step": "",
                },
            )
            return

        rows = [f"✅ {_label(d)}" for d in already_validated]
        rows += [f"📩 {_label(d)} — received, under review" for d in under_review]
        rows += [f"⚠️ {_label(d)} — still needed" for d in still_missing]
        body = "📋 Application checklist:\n" + "\n".join(rows)
        if still_missing:
            noun = "document" if len(still_missing) == 1 else "documents"
            body += (
                f"\n\n📤 Please share the remaining {len(still_missing)} "
                f"{noun} to move forward."
            )
        # Per project_optional_docs (2026-06-10): surface optional docs as a
        # separate "Optional" section so the SME knows they can send them but
        # isn't pressured to. Validated optionals appear up in the ✅ list
        # above (the expected set in the docs loop already includes them).
        optional_unsent = [
            d for d in DEFAULT_WHATSAPP_OPTIONAL_DOCS
            if d not in already_validated
        ]
        if optional_unsent:
            body += "\n\nℹ️ Optional (send if you have them):"
            for d in optional_unsent:
                body += f"\n• {_label(d)}"
        # Reuse the existing single_received template ({{ results }}) — it
        # already renders the body verbatim.
        await self._send(
            ctx, state, "onboarding.documents.single_received", {"results": body}
        )

    async def _send_more_docs_prompt(
        self, ctx: WorkflowContext, state: OnboardingState
    ) -> None:
        """Send the 'any more documents?' prompt. Tries interactive YES/NO
        reply buttons; falls back to the plain-text template when the button
        path (backend ``messages/interactive-buttons`` + MCP tool) isn't live
        — mirroring the CTA-URL fallback pattern. The button replies map to
        is_yes/is_no via the backend webhook (button_reply.title)."""
        sent = False
        send_buttons = getattr(self._msg, "send_reply_buttons", None)
        if send_buttons is not None and ctx.channel is Channel.WHATSAPP:
            try:
                sent = await send_buttons(
                    channel=_channel(ctx),
                    identity=ctx.identity,
                    template_key="onboarding.documents.more_docs_prompt",
                    buttons=[("more_docs_yes", "Yes"), ("more_docs_no", "No")],
                    locale=state.locale,
                )
            except Exception as exc:  # noqa: BLE001 — fall back to text
                ctx.logger.warning(
                    "more_docs_buttons.failed", error=str(exc)[:200]
                )
        if not sent:
            await self._send(ctx, state, "onboarding.documents.more_docs_prompt")

    async def _acknowledge_uploads(
        self,
        ctx: WorkflowContext,
        state: OnboardingState,
        validated: list[str],
        unprocessed: list[str],
        *,
        saw_zip: bool,
        missing_after: list[str],
    ) -> None:
        """Send a brief per-document receipt for this batch.

        Bug #16 design (UAT 2026-06-09, in-depth user analysis): per spec
        page 4 (Full Document Submission, second batch sample) and the
        user's literal preference, every upload turn shows only the
        ✅/⏳ batch receipts — never the cumulative ⚠️ checklist body.
        The full checklist appears in two places, both off this hot
        path:

          * ``documents_complete`` coffee message when the checklist
            naturally exhausts (the "end of upload session" the user
            asked for).
          * Self-service "what am I still missing?" reply (spec page 8
            "PENDING DOCS") wired in _documents_upload_loop_await.

        Genuine end-to-end failure (nothing ever validated AND this
        batch produced nothing) falls to ``documents.upload_failed``
        so the SME isn't silently dropped.
        """

        def _label(doc: str) -> str:
            return DOCUMENT_LABELS.get(doc, doc.replace("_", " ").title())

        all_required = list(DEFAULT_WHATSAPP_REQUIRED_DOCS)
        still_missing = list(missing_after)
        already_validated = [d for d in all_required if d not in still_missing]

        # Bug #1b (2026-06-09): if literally NOTHING has ever validated
        # AND this batch produced nothing either, the upload genuinely
        # failed end-to-end — send the honest "couldn't process" fallback.
        if not validated and not unprocessed and not already_validated:
            try:
                await self._send(ctx, state, "onboarding.documents.upload_failed")
            except Exception as exc:  # noqa: BLE001
                ctx.logger.warning(
                    "documents_upload_failed_ack.failed", error=str(exc)[:200]
                )
            return

        # Edge: every upload in this batch was a duplicate of an
        # already-validated doc. Stay silent (the SME will hear the next
        # ack when they send a NEW doc) — they can ask "what's missing?"
        # anytime to get the full state.
        if not validated and not unprocessed:
            return

        batch_rows = [f"✅ {_label(d)} — Received & Validated" for d in validated]
        batch_rows += [
            f"⏳ {_label(d)} — received, our team will review it"
            for d in unprocessed
        ]
        body = "\n".join(batch_rows)

        template_key = (
            "onboarding.documents.zip_received"
            if saw_zip
            else "onboarding.documents.single_received"
        )
        await self._send(ctx, state, template_key, {"results": body})

    async def _documents_complete(
        self, state: OnboardingState, ctx: WorkflowContext
    ) -> dict[str, Any]:
        # Coffee / "all documents received" exactly ONCE per run. This node is
        # re-entered every time the SME uploads an extra doc after completion;
        # _route_documents only routes here on the FIRST completion, but guard
        # here too so the message can never double-fire (user 2026-06-12).
        if state.documents_complete_sent:
            return self._step("documents_complete", ctx)
        await self._send(ctx, state, "onboarding.documents.complete")
        # Step 5 — all docs submitted, waiting for risk assessment.
        progress_step = await self._update_progress(state, ctx, step=5)
        return self._step(
            "documents_complete",
            ctx,
            documents_complete_sent=True,
            onboarding_progress_step=progress_step or state.onboarding_progress_step,
        )

    async def _more_docs_prompt_send(
        self, state: OnboardingState, ctx: WorkflowContext
    ) -> dict[str, Any]:
        """Per user (UAT 2026-06-10): after the coffee message, ask the SME
        whether they have any more documents to send. Covers the classifier-
        failure case (e.g. AoA hangs → marked unprocessed → count-based
        unblock advanced us anyway) and the "I forgot one" case so the SME
        can keep sending docs even after the loop has provisionally
        completed. YES → loops back to the upload-await node; NO → run
        proceeds to payment_wait.
        """

        await self._send(ctx, state, "onboarding.documents.more_docs_prompt")
        # Clear any stale decision from a previous trip through this prompt
        # so the router waits for THIS turn's reply.
        return self._step("more_docs_prompt_send", ctx, more_docs_decision=None)

    async def _more_docs_prompt_await(
        self, state: OnboardingState, ctx: WorkflowContext
    ) -> dict[str, Any]:
        reply = await_input({"waiting_for": "reply", "step": "more_docs_prompt"})
        if is_yes(reply):
            # Reset the upload counter so the count-based unblock fires
            # again only after this fresh batch meets the threshold.
            return self._step(
                "more_docs_prompt_await", ctx,
                more_docs_decision="yes",
                docs_uploaded_count=0,
            )
        if is_no(reply):
            return self._step("more_docs_prompt_await", ctx, more_docs_decision="no")
        # Off-script reply — answer in context and re-prompt.
        await self._smart_contextual(
            ctx, state, reply,
            "No problem — just reply YES if you have more documents to send, "
            "or NO if you're done. 🙂",
        )
        return self._step("more_docs_prompt_await", ctx, more_docs_decision=None)

    # -- Postman-triggered gates (pre-qualification + payment) ----------------

    @staticmethod
    def _is_prequalify_trigger(payload: Any) -> bool:
        if _extract_journey_status(payload) is not None:
            return True
        if isinstance(payload, dict):
            event = payload.get("event") or payload.get("event_type")
            return event in {
                "prequalification.completed",
                "eligibility.updated",
                "documents.completed",
            }
        return False

    @staticmethod
    def _is_payment_trigger(payload: Any) -> bool:
        if isinstance(payload, dict):
            event = payload.get("event") or payload.get("event_type")
            if event in {"madad_score.ready", "payment.requested"}:
                return True
        status = _extract_journey_status(payload)
        return status in {JourneyStatus.QUALIFIED, JourneyStatus.PRE_QUALIFIED}

    async def _prequalify_wait_await(
        self, state: OnboardingState, ctx: WorkflowContext
    ) -> dict[str, Any]:
        # PARK after the account-created message until the pre-qualification is
        # triggered (Postman in the demo). Any user chat meanwhile is answered,
        # not ignored — we never re-send or stall on it.
        payload = await_input({"waiting_for": "prequalification", "step": "prequalify_wait"})
        if self._is_prequalify_trigger(payload):
            # Step 4 — pre-qualified, full-doc checklist about to be sent.
            progress_step = await self._update_progress(state, ctx, step=4)
            return self._step(
                "prequalify_wait_await",
                ctx,
                prequalified=True,
                onboarding_progress_step=progress_step or state.onboarding_progress_step,
            )
        # Background poll / docs-settle tick (no SME text) — re-park silently so
        # a status_update heartbeat doesn't re-send the canned "pre-qualification
        # result will be ready soon" line every poll cycle (UAT 2026-06-13).
        if _is_inert_system_resume(payload):
            return self._step("prequalify_wait_await", ctx, prequalified=False)
        # Bug #7+#8 (2026-06-09): intent-route every off-script reply so each
        # type of question gets a meaningful answer instead of the same
        # canned "still pending" fallback every time the LLM is unavailable
        # (OpenAI 401 in QA showed this clearly).
        await self._contextual_off_script(
            ctx,
            state,
            payload,
            default_answer=(
                "Thanks! 🙌 Your pre-qualification result will be ready soon — "
                "I’ll share your document checklist here the moment it’s confirmed."
            ),
        )
        return self._step("prequalify_wait_await", ctx, prequalified=False)

    async def _payment_wait_await(
        self, state: OnboardingState, ctx: WorkflowContext
    ) -> dict[str, Any]:
        # Bug #12 (UAT 2026-06-09, Ishan diagnosis): when QUALIFIED+
        # arrived mid-docs-loop, the upstream docs-loop handler set
        # ``payment_ready=True`` on the SAME resume — but this node
        # used to call ``await_input`` unconditionally, parking until
        # a second event that backend never fires. Short-circuit when
        # we already have the trigger so the same event continues
        # straight into the payment chain (business_details_fetch →
        # products_list_fetch → payment_create → payment_send_link).
        if state.payment_ready:
            return self._step("payment_wait_await", ctx, payment_ready=True)
        # PARK after the coffee message until the payment step is triggered
        # (Postman in the demo). Capture the Madad score from the trigger payload.
        payload = await_input({"waiting_for": "payment_ready", "step": "payment_wait"})
        if self._is_payment_trigger(payload):
            score = _extract_madad_score(payload)
            return self._step(
                "payment_wait_await",
                ctx,
                payment_ready=True,
                **({"madad_score": score} if score is not None else {}),
            )
        # Background poll / docs-settle tick (no SME text) — re-park silently.
        # Without this the status-poller's ~60s status_update resume fell into
        # the off-script reply below and re-sent "You're all set…" every minute
        # after the coffee message (UAT 2026-06-13).
        if _is_inert_system_resume(payload):
            return self._step("payment_wait_await", ctx, payment_ready=False)
        await self._contextual_off_script(
            ctx,
            state,
            payload,
            default_answer=(
                "You’re all set — our team is finalising your assessment. I’ll "
                "share your Madad score and the next step here shortly. 🙂"
            ),
        )
        return self._step("payment_wait_await", ctx, payment_ready=False)

    # -- Step 7: status poll + payment ----------------------------------------

    async def _status_poll_on_demand(
        self, state: OnboardingState, ctx: WorkflowContext
    ) -> dict[str, Any]:
        # Long real-world wait may have elapsed; refresh token if stale.
        token, refresh, expires = await self._live_token(state, ctx)
        live_state = state if token == state.access_token else state.model_copy(
            update={"access_token": token}
        )
        status = await self._poll_journey_status(live_state)
        # Preserve the source set by the upstream await (webhook vs poll
        # trigger). Default to "poll" if nothing set it — this node is by
        # definition an active poll, and the first entry (from
        # documents_complete) has no upstream source.
        source = state.last_status_source or "poll"
        return self._step(
            "status_poll_on_demand",
            ctx,
            journey_status=status,
            last_status_source=source,
            last_polled_at=ctx.clock.now(),
            access_token=token, refresh_token=refresh, token_expires_at=expires,
        )

    async def _journey_wait_await(
        self, state: OnboardingState, ctx: WorkflowContext
    ) -> dict[str, Any]:
        # Suspend until a status_update / webhook resume arrives. The
        # dispatcher (translate_backend_event) maps well-known events to an
        # implied journey_status; capture it so the next poll routes
        # immediately rather than waiting for the backend to catch up.
        payload = await_input({"waiting_for": "journey_status", "step": "journey_wait"})
        help_template = _off_script_template(payload)
        if help_template is not None:
            await self._send(
                ctx,
                state,
                "onboarding.help.contextual",
                {"answer": self._answer_for(help_template), "next_step": _next_step_hint(state)},
            )
            return self._step(
                "journey_wait_await", ctx, last_status_source="chat"
            )
        if _is_casual_message(payload):
            await self._send(
                ctx,
                state,
                "onboarding.help.contextual",
                {"answer": "I’m here and tracking your application.", "next_step": _next_step_hint(state)},  # noqa: E501
            )
            return self._step(
                "journey_wait_await", ctx, last_status_source="chat"
            )
        if _is_portal_query(payload):
            await self._send(
                ctx,
                state,
                "onboarding.help.contextual",
                {
                    "answer": await self._safe_portal_answer(state),
                    "next_step": _next_step_hint(state),
                },
            )
            return self._step(
                "journey_wait_await", ctx, last_status_source="chat"
            )
        if _is_status_query(payload):
            await self._send(
                ctx,
                state,
                "onboarding.help.contextual",
                {
                    "answer": await self._safe_status_answer(state),
                    "next_step": _next_step_hint(state),
                },
            )
            return self._step(
                "journey_wait_await", ctx, last_status_source="chat"
            )
        source = _extract_status_source(payload)
        fields: dict[str, Any] = {"last_status_source": source}
        forced = _extract_journey_status(payload)
        if forced is not None:
            fields["journey_status"] = forced
        # The offer.selected / credit_line.activated webhooks carry the
        # lender + terms; capture them so the ✅ confirmation and 🎊 activation
        # messages can name the bank without an extra /me round-trip.
        sel = _selected_offer_from_payload(payload)
        if sel:
            fields["selected_offer"] = sel
        return self._step("journey_wait_await", ctx, **fields)

    async def _not_qualified(
        self, state: OnboardingState, ctx: WorkflowContext
    ) -> dict[str, Any]:
        await self._send(ctx, state, "onboarding.not_qualified")
        return self._step("not_qualified", ctx, outcome="not_qualified")

    async def _business_details_fetch(
        self, state: OnboardingState, ctx: WorkflowContext
    ) -> dict[str, Any]:
        # Bug #13 (UAT 2026-06-09, Ishan diagnosis): the payment branch
        # used to read ``state.access_token`` directly, which is the same
        # token minted ~15 minutes earlier at the doc-upload phase. After
        # the docs review + admin qualify window the token had expired,
        # the backend returned HTTP 401, retries exhausted, the run
        # died, no payment link reached the SME. Mint on demand here
        # (and in every other payment-branch node) so the credentials
        # are always live by the time we hit Madad's APIs.
        token, refresh, expires = await self._live_token(state, ctx)
        if not token:
            return self._step(
                "business_details_fetch", ctx,
                access_token=token, refresh_token=refresh, token_expires_at=expires,
            )
        result = await self._pay.get_business_details(access_token=token)
        business_id = (
            result.get("business_details_id") if isinstance(result, dict) else None
        )
        return self._step(
            "business_details_fetch", ctx,
            business_details_id=business_id,
            access_token=token, refresh_token=refresh, token_expires_at=expires,
        )

    async def _products_list_fetch(
        self, state: OnboardingState, ctx: WorkflowContext
    ) -> dict[str, Any]:
        token, refresh, expires = await self._live_token(state, ctx)
        if not token:
            return self._step(
                "products_list_fetch", ctx,
                access_token=token, refresh_token=refresh, token_expires_at=expires,
            )
        result = await self._pay.list_monetization_products(access_token=token)
        products = (
            list(result.get("products", [])) if isinstance(result, dict) else []
        )
        product = products[0] if products else {}
        return self._step(
            "products_list_fetch",
            ctx,
            payment_product_id=product.get("product_id"),
            access_token=token, refresh_token=refresh, token_expires_at=expires,
        )

    async def _payment_create(
        self, state: OnboardingState, ctx: WorkflowContext
    ) -> dict[str, Any]:
        token, refresh, expires = await self._live_token(state, ctx)
        if not (token and state.business_details_id and state.payment_product_id):
            return self._step(
                "payment_create", ctx,
                access_token=token, refresh_token=refresh, token_expires_at=expires,
            )
        key = f"{ctx.run_id}:create_monetization_payment"
        result = await self._pay.create_monetization_payment(
            access_token=token,
            business_details_id=state.business_details_id,
            product_id=state.payment_product_id,
            amount_qar=ONBOARDING_FEE_QAR,
            idempotency_key=key,
        )
        payment_id = result.get("payment_id") if isinstance(result, dict) else None
        payment_status = result.get("status") if isinstance(result, dict) else None
        # The CREATE response carries the real Tess checkout URL on the
        # ``paymentLink`` field. Capture it now so payment_send_link can
        # deliver it directly via our messenger — independent of the
        # backend's notification provider (which 502s in UAT).
        payment_link = (
            result.get("paymentLink") or result.get("payment_link")
            if isinstance(result, dict)
            else None
        )
        provider_ref = (
            result.get("providerOrderNumber") if isinstance(result, dict) else None
        )
        return self._step(
            "payment_create",
            ctx,
            payment_id=payment_id,
            payment_status=payment_status,
            payment_link=payment_link,
            payment_provider_ref=provider_ref,
            access_token=token, refresh_token=refresh, token_expires_at=expires,
            idempotency_keys={
                **state.idempotency_keys,
                "create_monetization_payment": key,
            },
        )

    async def _payment_send_link(
        self, state: OnboardingState, ctx: WorkflowContext
    ) -> dict[str, Any]:
        # The payment link is ALREADY on state from payment_create — Madad's
        # CREATE response returns the Tess checkout URL on ``paymentLink``.
        # We send it via our messenger directly; we DO NOT depend on the
        # backend's send-monetization-payment-link tool (it routes through
        # an upstream notification provider that has been flaky in UAT and
        # adds no value over our own send).
        amount = f"{ONBOARDING_FEE_QAR:,}"
        score = state.madad_score if state.madad_score is not None else 78
        variables = {
            "amount":         amount,
            "score":          score,
            "payment_link":   state.payment_link or "",
            "provider_ref":   state.payment_provider_ref or "",
        }
        # Spec Step 5: a tappable "Pay QAR 6,000 →" button (interactive CTA-URL)
        # instead of a raw link. Falls back to the plain-text message (with the
        # link inline) if the interactive path isn't available.
        sent_as_button = False
        if ctx.channel is Channel.WHATSAPP and state.payment_link:
            try:
                sent_as_button = await self._msg.send_cta_url(
                    channel=_channel(ctx),
                    identity=ctx.identity,
                    template_key="onboarding.payment.request.button",
                    button_text=f"Pay QAR {amount} →",
                    button_url=state.payment_link,
                    variables=variables,
                    locale=state.locale,
                )
            except Exception as exc:  # noqa: BLE001 — fall back to text
                ctx.logger.warning(
                    "payment_send_link.cta_failed",
                    error=str(exc)[:200],
                    note="falling back to plain-text payment message",
                )
        if not sent_as_button:
            await self._send(ctx, state, "onboarding.payment.request", variables)
        # Step 6 — Madad score + payment gate sent to user.
        await self._update_progress(state, ctx, step=6)
        # A9 (Ishan 2026-06-07): per the PDF Step 5 nudge spec, every payment-
        # pending nudge re-sends the payment link. The nudge templates
        # (nudge.payment_pending.{1,2,3}) substitute {{ amount }} and
        # {{ payment_link }} at dispatch time; thread the live values
        # through to the scheduler so each scheduled step renders correctly.
        await self._reminders.schedule(
            "payment_pending",
            channel=_channel(ctx),
            identity=ctx.identity,
            target_ref=state.madad_user_id or ctx.session_id,
            variables={
                "amount":       amount,
                "payment_link": state.payment_link or "",
            },
        )
        # ALSO fire the backend's notification trigger as a side-channel —
        # if it succeeds the SME gets a Madad-branded copy of the link too,
        # if it fails (502 in current UAT) we already sent our own.
        # Bug #13 (UAT 2026-06-09): mint a fresh token first — the cached
        # token may be ~15 minutes old by now.
        token, refresh, expires = await self._live_token(state, ctx)
        if token and state.payment_id:
            key = f"{ctx.run_id}:send_monetization_payment_link"
            try:
                await self._pay.send_monetization_payment_link(
                    access_token=token,
                    payment_id=state.payment_id,
                    channel=_channel(ctx),
                    identity=ctx.identity,
                    idempotency_key=key,
                )
                return self._step(
                    "payment_send_link", ctx,
                    access_token=token, refresh_token=refresh, token_expires_at=expires,
                    idempotency_keys={
                        **state.idempotency_keys,
                        "send_monetization_payment_link": key,
                    },
                )
            except Exception as exc:  # noqa: BLE001
                ctx.logger.warning(
                    "payment_send_link.notification_failed",
                    error=str(exc)[:200],
                    note="primary link already sent via messenger — continuing",
                )
        return self._step(
            "payment_send_link", ctx,
            access_token=token, refresh_token=refresh, token_expires_at=expires,
        )

    async def _payment_await(
        self, state: OnboardingState, ctx: WorkflowContext
    ) -> dict[str, Any]:
        result = await_input({"waiting_for": "payment", "step": "payment"})
        help_template = _off_script_template(result)
        if help_template is not None:
            await self._send(
                ctx,
                state,
                "onboarding.help.contextual",
                {"answer": self._answer_for(help_template), "next_step": _next_step_hint(state)},
            )
            await self._send(ctx, state, "onboarding.payment.awaiting")
            return self._step("payment_await", ctx, paid=False)
        if _is_status_query(result):
            await self._send(ctx, state, "onboarding.payment.awaiting")
            return self._step("payment_await", ctx, paid=False)
        paid = bool(result.get("paid")) if isinstance(result, dict) else False
        if paid:
            await self._reminders.suppress(
                target_ref=state.madad_user_id or ctx.session_id
            )
            # Step 7 — payment received, application forwarded to banks.
            await self._update_progress(state, ctx, step=7)
            # A8a: PDF Step 6 — "Thank you — payment received! Forwarded to
            # <banks>". Read the assigned banks from BusinessDetails.banksToSend
            # (per Ishan 2026-06-07: populates when admin sets QUALIFIED /
            # forwards). Fail-safe to the empty list so the message still goes
            # out — minus the bank-list line.
            banks = await self._fetch_banks_to_send(state, ctx)
            # UAT 2026-06-10 screenshot: "(Ref: )" rendered empty because
            # state.application_ref was never populated for the SIGN_UP
            # paths that don't capture session.reference_number. Fetch
            # from /me as a fallback so the SME always sees their ref
            # number on the payment-confirmed message.
            ref = state.application_ref
            if not ref:
                token = (await self._live_token(state, ctx))[0]
                if token:
                    try:
                        info = await self._identity.me(access_token=token)
                        ref = _extract_reference_from_me(info)
                    except Exception as exc:  # noqa: BLE001
                        ctx.logger.warning(
                            "payment_confirmed.ref_fetch_failed",
                            error=str(exc)[:200],
                        )
            await self._send(
                ctx,
                state,
                "onboarding.payment.confirmed",
                {
                    "banks":     _format_banks_list(banks),
                    "ref":       ref or "",
                    "bank_count": len(banks),
                },
            )
            return self._step(
                "payment_await", ctx,
                paid=paid,
                application_ref=ref or state.application_ref,
            )
        return self._step("payment_await", ctx, paid=paid)

    async def _fetch_banks_to_send(
        self, state: OnboardingState, ctx: WorkflowContext
    ) -> list[str]:
        """Read \`BusinessDetails.banksToSend\` for the current SME — the list of
        banks the admin marked as targets when forwarding the application.
        Tolerates camelCase / snake_case / list-or-string shapes."""
        if not state.access_token:
            return []
        try:
            # MonetizationPaymentClient also wraps madad_kyc_get_business_details
            # (it needs business_details_id for payment writes); reusing that
            # avoids adding a parallel method on KycClient.
            result = await self._pay.get_business_details(access_token=state.access_token)
        except Exception as exc:  # noqa: BLE001 — degrade in staging
            ctx.logger.warning(
                "business_details.banks_fetch_failed",
                error=str(exc)[:200],
                note="continuing without bank list",
            )
            return []
        if not isinstance(result, dict):
            return []
        # The cluster adapter unwraps to ``business_details_id`` flat dict;
        # main also stashes the raw camelCase response so look both places.
        candidates: list[Any] = []
        raw = result.get("banksToSend") or result.get("banks_to_send")
        if raw is not None:
            candidates.append(raw)
        for key in ("businessDetails", "business_details"):
            nested = result.get(key)
            if isinstance(nested, dict):
                inner = nested.get("banksToSend") or nested.get("banks_to_send")
                if inner is not None:
                    candidates.append(inner)
        banks: list[str] = []
        for value in candidates:
            if isinstance(value, list):
                banks.extend(str(b) for b in value if b)
                break
            if isinstance(value, str) and value.strip():
                # The backend JSONB column might come back as a comma-separated
                # string in some edge cases — split it.
                banks.extend([s.strip() for s in value.split(",") if s.strip()])
                break
        return banks

    # -- Step 8: lender + offers + terminals ----------------------------------

    async def _lender_status_poll(
        self, state: OnboardingState, ctx: WorkflowContext
    ) -> dict[str, Any]:
        token, refresh, expires = await self._live_token(state, ctx)
        live_state = state if token == state.access_token else state.model_copy(
            update={"access_token": token}
        )
        status = await self._poll_journey_status(live_state)
        source = state.last_status_source or "poll"
        return self._step(
            "lender_status_poll",
            ctx,
            journey_status=status,
            last_status_source=source,
            last_polled_at=ctx.clock.now(),
            access_token=token, refresh_token=refresh, token_expires_at=expires,
        )

    async def _lender_wait_await(
        self, state: OnboardingState, ctx: WorkflowContext
    ) -> dict[str, Any]:
        payload = await_input({"waiting_for": "journey_status", "step": "lender_wait"})
        help_template = _off_script_template(payload)
        if help_template is not None:
            await self._send(
                ctx,
                state,
                "onboarding.help.contextual",
                {"answer": self._answer_for(help_template), "next_step": _next_step_hint(state)},
            )
            return self._step("lender_wait_await", ctx, last_status_source="chat")
        if _is_casual_message(payload):
            await self._send(
                ctx,
                state,
                "onboarding.help.contextual",
                {"answer": "I’m here and tracking your lender review.", "next_step": _next_step_hint(state)},  # noqa: E501
            )
            return self._step("lender_wait_await", ctx, last_status_source="chat")
        if _is_portal_query(payload):
            await self._send(
                ctx,
                state,
                "onboarding.help.contextual",
                {
                    "answer": await self._safe_portal_answer(state),
                    "next_step": _next_step_hint(state),
                },
            )
            return self._step("lender_wait_await", ctx, last_status_source="chat")
        if _is_status_query(payload):
            await self._send(
                ctx,
                state,
                "onboarding.help.contextual",
                {
                    "answer": await self._safe_status_answer(state),
                    "next_step": _next_step_hint(state),
                },
            )
            return self._step("lender_wait_await", ctx, last_status_source="chat")
        source = _extract_status_source(payload)
        fields: dict[str, Any] = {"last_status_source": source}
        forced = _extract_journey_status(payload)
        if forced is not None:
            fields["journey_status"] = forced
        return self._step("lender_wait_await", ctx, **fields)

    async def _offers_fetch(
        self, state: OnboardingState, ctx: WorkflowContext
    ) -> dict[str, Any]:
        # The backend embeds the lender offer set on the user record once the
        # journey reaches ACCEPTED. UAT 2026-06-10: the enriched /me payload
        # exposes them under ``offersReceived`` (Ishan handover §8), not
        # ``offers`` — earlier code rendered the offers template with empty
        # cards because the field name didn't match. Try the registered
        # payload first (already on state if check_registration ran),
        # otherwise fetch fresh.
        offers: list[dict[str, Any]] = []
        if isinstance(state.registration_payload, dict):
            raw = state.registration_payload.get("offers")
            if isinstance(raw, list):
                offers = [o for o in raw if isinstance(o, dict)]
        token, refresh, expires = await self._live_token(state, ctx)
        if not offers and token:
            try:
                info = await self._identity.me(access_token=token)
                offers = _extract_offers_from_me(info)
            except Exception as exc:  # noqa: BLE001 — degrade gracefully
                ctx.logger.warning(
                    "offers_fetch.me_failed", error=str(exc)[:200]
                )
        # Step 8 — offers ready, handing off to platform.
        progress_step = await self._update_progress(state, ctx, step=8)
        return self._step(
            "offers_fetch",
            ctx,
            offers=offers,
            access_token=token, refresh_token=refresh, token_expires_at=expires,
            onboarding_progress_step=progress_step or state.onboarding_progress_step,
        )

    async def _offer_view_send(
        self, state: OnboardingState, ctx: WorkflowContext
    ) -> dict[str, Any]:
        # A6 (Ishan 2026-06-07): render PDF Step 8 structured offer cards —
        # one block per offer with lender + credit limit + interest rate +
        # tenure + processing fee. Falls back to a count-only line if the
        # offer list is empty (auth_me hasn't surfaced offers yet).
        #
        # Re-send only when the offer SET changed (a new lender made an offer) —
        # the fast status poller re-enters this route ~every minute while the SME
        # is deciding, and we must not re-spam the same cards each tick.
        if _offers_sig(state.offers) == state.offers_shown_sig:
            return self._step("offer_view_send", ctx)
        await self._send(
            ctx,
            state,
            "onboarding.offers.preview",
            {
                "count": len(state.offers),
                "offer_cards": _format_offer_cards(state.offers),
            },
        )
        return self._step("offer_view_send", ctx)

    async def _offer_handoff_to_madad(
        self, state: OnboardingState, ctx: WorkflowContext
    ) -> dict[str, Any]:
        # Skip the whole handoff block on a routine poll where the offer set is
        # unchanged (the run just passed through offer_view_send without
        # re-sending). Only (re)send the button when new offers were just shown.
        if _offers_sig(state.offers) == state.offers_shown_sig:
            return self._step("offer_handoff_to_madad", ctx, outcome="offer_handoff")
        # PDF Step 8 — tappable "Login to Madad →" CTA-URL button on WhatsApp
        # (Meta caps the label at 20 chars). Falls back to the plain-text
        # template (with the URL inline) if the interactive path fails.
        portal_url = "https://uat-portal.madadfintech.com"
        sent_as_button = False
        if ctx.channel is Channel.WHATSAPP:
            try:
                sent_as_button = await self._msg.send_cta_url(
                    channel=_channel(ctx),
                    identity=ctx.identity,
                    template_key="onboarding.offer.handoff.button",
                    button_text="Login to Madad →",
                    button_url=portal_url,
                    variables={},
                    locale=state.locale,
                )
            except Exception as exc:  # noqa: BLE001 — fall back to text
                ctx.logger.warning(
                    "offer_handoff.cta_failed",
                    error=str(exc)[:200],
                    note="falling back to plain-text handoff message",
                )
        if not sent_as_button:
            await self._send(ctx, state, "onboarding.offer.handoff")
        # Record the shown offer set so routine polls don't re-send these cards.
        return self._step(
            "offer_handoff_to_madad", ctx, outcome="offer_handoff",
            offers_shown_sig=_offers_sig(state.offers),
        )

    async def _offer_confirmed(
        self, state: OnboardingState, ctx: WorkflowContext
    ) -> dict[str, Any]:
        # The SME selected an offer in the Madad portal (backend offer.selected
        # webhook → OFFER_ACCEPTED). Send the one-time ✅ confirmation, then park
        # (the edge back to journey_wait_await) for the credit-line activation.
        # Guarded so a later background poll that still reports OFFER_ACCEPTED
        # can't re-send the confirmation.
        if state.offer_confirmed_sent:
            return self._step("offer_confirmed", ctx)
        offer = state.selected_offer or {}
        lender = _lender_name(offer) or "your selected bank"
        await self._send(
            ctx, state, "onboarding.offer.confirmed", {"lender": lender}
        )
        return self._step("offer_confirmed", ctx, offer_confirmed_sent=True)

    async def _activated(
        self, state: OnboardingState, ctx: WorkflowContext
    ) -> dict[str, Any]:
        # A8b: PDF Step 9 — render the lender / limit / rate / tenure on the
        # activation message. UAT 2026-06-10: the prior code only inspected
        # ``offers`` on /me which is the wrong field name (it's
        # ``offersReceived``), and didn't read the actual ``creditLines``
        # entry at all — so the activated message showed empty details
        # even when the line was active. Preference order now:
        #   1. state.selected_offer (offer-handoff flow set it)
        #   2. state.registration_payload.creditLine (if check_registration
        #      already returned an active line)
        #   3. /me creditLines (most authoritative for an ACTIVE state)
        #   4. /me offersReceived → look for an ACCEPTED offer / fall back to first
        offer: dict[str, Any] = state.selected_offer or {}
        offers = list(state.offers)
        credit_line: dict[str, Any] = {}
        if isinstance(state.registration_payload, dict):
            cl = state.registration_payload.get("creditLine")
            if isinstance(cl, dict):
                credit_line = cl

        token, refresh, expires = await self._live_token(state, ctx)
        if not (offer or credit_line) and token:
            try:
                info = await self._identity.me(access_token=token)
                credit_line = credit_line or _extract_credit_line_from_me(info)
                if not offers:
                    offers = _extract_offers_from_me(info)
            except Exception as exc:  # noqa: BLE001 — degrade in staging
                ctx.logger.warning(
                    "activated.me_failed", error=str(exc)[:200]
                )
        if not offer and offers:
            for cand in offers:
                if isinstance(cand, dict):
                    status = str(
                        cand.get("status") or cand.get("offerStatus") or ""
                    ).upper()
                    if "ACCEPT" in status:
                        offer = cand
                        break
            if not offer:
                first = offers[0] if offers else {}
                offer = first if isinstance(first, dict) else {}

        # Read each field from credit_line first (Step 9 ground truth),
        # offer second (handoff time), then fall back to "—".
        def _from_any(*keys: str) -> Any:
            for src in (credit_line, offer):
                for k in keys:
                    if k in src and src[k] is not None:
                        return src[k]
            return None

        lender = _from_any("lender", "lenderName", "bank_name", "bankName") or "your bank"
        try:
            limit = f"QAR {int(_from_any('creditLimit', 'credit_limit', 'limit') or 0):,}"
        except (TypeError, ValueError):
            limit = "QAR —"
        try:
            rate = f"{float(_from_any('interestRate', 'interest_rate', 'rate') or 0):g}%"
        except (TypeError, ValueError):
            rate = "—"
        try:
            tenure = f"{int(_from_any('tenureDays', 'tenure_days', 'tenure') or 0)} days"
        except (TypeError, ValueError):
            tenure = "—"

        # Bug #58 (UAT 2026-06-13): the OFFER_ACCEPTED → ACTIVATED transition can
        # land faster than the agent resumes/polls, so the run routes straight to
        # 'activated' and the one-time ✅ "offer confirmed" acceptance message
        # never fires (the SME got activation but not the acceptance). If it
        # hasn't been sent, backfill it HERE — in order, just before the
        # activation message — so the acceptance is never silently skipped.
        backfilled_offer_confirmed = False
        if not state.offer_confirmed_sent:
            try:
                await self._send(
                    ctx, state, "onboarding.offer.confirmed", {"lender": lender}
                )
                backfilled_offer_confirmed = True
            except Exception as exc:  # noqa: BLE001
                ctx.logger.warning(
                    "activated.offer_confirmed_backfill_failed", error=str(exc)[:200]
                )

        await self._send(
            ctx,
            state,
            "onboarding.activated",
            {
                "lender": lender,
                "limit": limit,
                "rate": rate,
                "tenure": tenure,
                "ref": state.application_ref or "",
            },
        )
        return self._step(
            "activated", ctx,
            outcome="completed",
            offer_confirmed_sent=state.offer_confirmed_sent or backfilled_offer_confirmed,
            access_token=token, refresh_token=refresh, token_expires_at=expires,
        )

    async def _invoice_collect_await(
        self, state: OnboardingState, ctx: WorkflowContext
    ) -> dict[str, Any]:
        # Steps 10-13: once the credit line is ACTIVE, the SME can send invoices
        # over WhatsApp. Each uploaded PDF/photo is submitted for financing
        # immediately — same extraction + submission path as the portal — and
        # the run stays parked here to accept more. Disbursement / repayment
        # alerts then arrive via the backend webhooks (transaction.disbursed,
        # repayment.*). Non-file chat is answered in context; we never drop it.
        reply = await_input({"waiting_for": "invoice", "step": "invoice_collect"})
        attachments = _valid_upload_attachments(reply)
        if not attachments:
            await self._smart_contextual(
                ctx,
                state,
                reply,
                "Whenever you have an invoice to finance, just send it here as a "
                "PDF or photo and I'll submit it for financing right away. 🙂",
            )
            return self._step("invoice_collect_await", ctx)

        # Mint a live token from the verified identity (a long-active user can
        # resume with an empty/expired cached token — same fix as doc uploads).
        token, refresh, expires = await self._live_token(state, ctx)
        submitted = 0
        failed = 0
        for att in attachments:
            content = att.get("content_base64") or ""
            filename = att.get("filename") or "invoice.pdf"
            if not content or not token:
                failed += 1
                continue
            try:
                await self._kyc.upload_invoice_base64(
                    access_token=token,
                    content_base64=content,
                    filename=filename,
                    mime_type=att.get("mime_type"),
                )
                submitted += 1
            except Exception as exc:  # noqa: BLE001 — never crash the run
                ctx.logger.warning(
                    "invoice_upload.failed",
                    filename=filename,
                    error=str(exc)[:200],
                )
                failed += 1

        if submitted:
            noun = "your invoice has" if submitted == 1 else f"{submitted} invoices have"
            answer = (
                f"✅ Got it — {noun} been submitted for financing. Our team will "
                "review and you'll get an update here once it's disbursed. Send "
                "another invoice anytime. 🙂"
            )
        else:
            answer = (
                "I couldn't read that file just now — please resend the invoice as "
                "a clear PDF or photo and I'll submit it for financing."
            )
        # Send the confirmation directly via the existing contextual template
        # (no new CMS template needed; no LLM round-trip for a confirmation).
        await self._send(
            ctx, state, "onboarding.help.contextual", {"answer": answer, "next_step": ""}
        )
        return self._step(
            "invoice_collect_await",
            ctx,
            access_token=token,
            refresh_token=refresh,
            token_expires_at=expires,
        )

    # -- routers --------------------------------------------------------------

    def _route_entry(self, state: OnboardingState) -> str:
        if state.entry_reply == "YES":
            return "check_contact"
        if state.entry_reply == "NO":
            return "declined"
        return "ask_again"

    def _route_check_contact(self, state: OnboardingState) -> str:
        # Per Ishan (cluster e6ea5d2, 2026-06-10): if
        # ``madad_mcp_check_registration`` returned a route hint, the lead
        # is a returning user — re-send the appropriate message instead
        # of silently re-onboarding them (Bug #2 + Bug #6).
        if state.registration_route:
            return "registered_routed"
        result: Any = state.check_contact_result
        if result is None:
            return self._new_lead_route(state)
        # LangGraph's checkpointer may round-trip the Pydantic model as a
        # plain dict between nodes. Read both shapes so the router doesn't
        # silently fall through to "new" when the cluster confirmed an
        # existing user.
        if isinstance(result, dict):
            exists = bool(result.get("exists", False))
            domain_exists = bool(
                result.get("domain_exists") or result.get("domainExists", False)
            )
        else:
            exists = bool(getattr(result, "exists", False))
            domain_exists = bool(getattr(result, "domain_exists", False))
        if exists:
            return "existing"
        if domain_exists:
            return "blocked"
        return self._new_lead_route(state)

    def _new_lead_route(self, state: OnboardingState) -> str:
        """Per Ishan (2026-06-07): WhatsApp organic-entry new leads skip the
        collect_details + complete_onboarding hops. The single
        ``open_session(create_user_if_missing=True)`` call mints a SIGN_UP
        account from the phone alone and returns the access_token directly.
        Email new-leads still need the full path because the backend can't
        infer business identity from an email address alone."""
        return "new_whatsapp" if state.channel is Channel.WHATSAPP else "new"

    def _route_channel_session(self, state: OnboardingState) -> str:
        # Phase 6 (invoice financing) will branch existing users into a
        # fast-path; Phase 2 always proceeds to consent + CR.
        return "consent"

    def _route_consent_upload(self, state: OnboardingState) -> str:
        return "uploaded" if state.consent and state.cr_ref else "missing"

    def _route_eligibility_status(self, state: OnboardingState) -> str:
        return "eligible" if state.eligible else "ineligible"

    def _route_eligibility_intake(self, state: OnboardingState) -> str:
        return "received" if state.eligibility_form_data else "missing"

    def _route_financials_upload(self, state: OnboardingState) -> str:
        return "uploaded" if state.financials_received else "missing"

    def _route_buyer(self, state: OnboardingState) -> str:
        return "received" if state.buyers else "missing"

    def _route_shareholders(self, state: OnboardingState) -> str:
        return "received" if state.shareholders else "missing"

    def _route_documents(self, state: OnboardingState) -> str:
        # Refinement per Ishan (UAT 2026-06-09): when admin QUALIFIES
        # mid-docs-loop, jump STRAIGHT to the payment chain. The
        # ``documents_complete`` coffee message ("🎊 all documents
        # received") is misleading in that case — the checklist isn't
        # actually complete; admin overrode it. ``payment_ready`` is
        # set on the same forced-status branch in
        # _documents_upload_loop_await, so this route catches the
        # override and bypasses both ``documents_complete`` and the
        # short-circuited ``payment_wait_await`` stop.
        if state.payment_ready or state.journey_status in {
            JourneyStatus.QUALIFIED,
            JourneyStatus.ACCEPTED,
            JourneyStatus.OFFER_ACCEPTED,
            JourneyStatus.ACTIVATED,
        }:
            return "payment"
        # Frustrated-user escape hatch: the SME replied NO to "any more
        # documents?" while some required docs were still undetected — proceed
        # to the next step (the payment-wait park) without the coffee.
        if state.docs_proceed:
            return "proceed"
        # TRUE completion: every required doc detected. Show the coffee /
        # "all documents received" message exactly ONCE (user 2026-06-12),
        # then re-park silently. A classifier hang that leaves a required slot
        # pending does NOT auto-complete here — instead the in-loop "any more
        # documents?" prompt fires and the SME replies NO to proceed.
        if not state.missing_documents:
            return "complete" if not state.documents_complete_sent else "await_again"
        return "await_again"

    def _route_more_docs(self, state: OnboardingState) -> str:
        decision = (state.more_docs_decision or "").lower()
        if decision == "yes":
            return "yes"
        if decision == "no":
            return "no"
        return "await_again"

    def _route_prequalify_wait(self, state: OnboardingState) -> str:
        return "go" if state.prequalified else "wait"

    def _route_payment_wait(self, state: OnboardingState) -> str:
        # Bug #12 (UAT 2026-06-09): the payment trigger is the same
        # ``madad_score.ready`` event that exits the docs loop. When it
        # arrives mid-docs-loop, the docs-loop handler consumes it,
        # parks here, and the SME used to be stuck until a second
        # qualify fired. payment_ready is now set on that same
        # transition (see _documents_upload_loop_await); the journey-
        # status check below is a belt-and-braces safety net for any
        # other arrival path that advanced the journey past pre-qual
        # without explicitly toggling payment_ready.
        if state.payment_ready:
            return "go"
        if state.journey_status in {
            JourneyStatus.QUALIFIED,
            JourneyStatus.ACCEPTED,
            JourneyStatus.OFFER_ACCEPTED,
            JourneyStatus.ACTIVATED,
        }:
            return "go"
        return "wait"

    def _route_payment(self, state: OnboardingState) -> str:
        return "paid" if state.paid else "unpaid"

    def _route_journey_status(self, state: OnboardingState) -> str:
        s = state.journey_status
        if s in (JourneyStatus.PRE_QUALIFIED, JourneyStatus.QUALIFIED):
            return "payment"
        if s == JourneyStatus.IN_ELIGIBLE:
            return "ineligible"
        if s in (JourneyStatus.UNQUALIFIED, JourneyStatus.NOT_ACCEPTED):
            return "unqualified"
        # ACCEPTED = a lender just made (another) offer → (re)show ALL offers.
        # OFFER_ACCEPTED = the SME picked one in the portal → ✅ confirmation.
        # Split so a portal selection doesn't re-spam the offer list.
        if s == JourneyStatus.ACCEPTED:
            return "offers"
        if s == JourneyStatus.OFFER_ACCEPTED:
            return "offer_confirmed"
        if s == JourneyStatus.ACTIVATED:
            return "activated"
        return "wait"

    def _route_status_resume(self, state: OnboardingState) -> str:
        return "await_again" if state.last_status_source == "chat" else "poll"

    def _route_invoice_collect(self, state: OnboardingState) -> str:
        # ACTIVE users stay parked collecting invoices: always loop back to the
        # await node so each new invoice (or chat) is handled in turn.
        return "loop"

    # -- helpers --------------------------------------------------------------

    def _answer_for(self, template_key: str) -> str:
        if template_key == "onboarding.help.security":
            return (
                "Yes, this is legitimate. Madad is a regulated business finance "
                "company in Qatar. The consent only lets us use your business "
                "information and documents to assess financing eligibility."
            )
        if template_key == "onboarding.help.what_is_madad":
            return (
                "Madad helps Qatar businesses unlock working capital from unpaid "
                "invoices owed by enterprise or government clients. We assess your "
                "business, collect required documents, and connect you with financing offers."
            )
        return "I can help with that."

    async def _safe_status_answer(self, state: OnboardingState) -> str:
        status = state.journey_status.value if state.journey_status else None
        if state.access_token:
            try:
                info = await self._identity.me(access_token=state.access_token)
                if isinstance(info, dict):
                    status = (
                        info.get("journeyStatus")
                        or (info.get("user") or {}).get("journeyStatus")
                        or status
                    )
            except Exception:
                pass
        if status:
            return f"Your Madad application status is {status}. I’ll keep guiding you here as the next step becomes available."  # noqa: E501
        return "Your Madad application is in progress. I’ll keep guiding you here as the next step becomes available."  # noqa: E501

    async def _safe_portal_answer(self, state: OnboardingState) -> str:
        unique_id = None
        if state.access_token:
            try:
                info = await self._identity.me(access_token=state.access_token)
                if isinstance(info, dict):
                    nested = info.get("user")
                    user = nested if isinstance(nested, dict) else info
                    unique_id = user.get("uniqueId") or user.get("unique_id")
            except Exception:  # noqa: BLE001
                pass
        prefix = f"Your Madad ID is {unique_id}. " if unique_id else ""
        return (
            f"{prefix}For security, I can only share the SME-facing Madad portal: "
            "madadfintech.com. I cannot share admin or lender portal links in this chat."
        )

    async def _live_token(
        self, state: OnboardingState, ctx: WorkflowContext
    ) -> tuple[str | None, str | None, int | None]:
        """Return a non-expired ``(access_token, refresh_token, expires_at)``.

        Production journeys span days; the Madad access_token only lives 900s.
        Whenever the existing token is within 60s of expiry (or already
        past), re-open the channel session to mint a fresh one. The new
        tuple is returned for the node to write back into state via its
        return dict so subsequent turns inherit the live credentials.

        On refresh failure the stale tuple is returned and the downstream
        MCP call will 401 — which our tolerant wrappers absorb. Better to
        keep moving than crash the run.
        """

        token = state.access_token
        refresh = state.refresh_token
        expires = state.token_expires_at
        now_ts = ctx.clock.now().timestamp()
        # Fast path: a cached token with a KNOWN expiry that is still
        # comfortably valid. A None/unknown expiry is NOT treated as
        # valid-forever (the old bug) — an onboarding run can be parked for
        # days/weeks while the SME collects documents, so an unknown-expiry
        # cached token is almost certainly stale. When in doubt, re-mint.
        if token and expires is not None and expires - now_ts > 60:
            return token, refresh, expires
        # Otherwise MINT ON DEMAND. The WhatsApp identity is already verified
        # (Meta-signed inbound webhook + a WhatsApp-verified number), so the
        # backend mints a fresh agent access token from the identity alone — no
        # password, no login. This covers: no token in state, unknown expiry,
        # and the near/after-expiry case — so an upload that arrives 5 days
        # after YES still gets a live token instead of a 401. (Root cause of
        # docs silently not persisting, 2026-06-12.)
        try:
            session = await self._identity.open_session(
                channel=_channel(ctx),
                identifier=ctx.identity,
                create_onboarding_token=False,
            )
            ctx.logger.info(
                "token.minted",
                had_cached_token=bool(token),
                old_expires_at=expires,
                new_expires_at=session.token_expires_at,
            )
            return (
                session.access_token or token,
                session.refresh_token or refresh,
                session.token_expires_at or expires,
            )
        except Exception as exc:  # noqa: BLE001
            ctx.logger.warning("token.mint_failed", error=str(exc)[:200])
            return token, refresh, expires

    async def _poll_journey_status(
        self, state: OnboardingState
    ) -> JourneyStatus | None:
        # If a recent webhook supplied a journey_status (last_status_source
        # is "webhook" and state.journey_status is already set), trust it
        # — the operator's event IS the truth in staging, and re-polling
        # auth_me would just overwrite with a stale backend snapshot.
        if (
            state.last_status_source == "webhook"
            and state.journey_status is not None
        ):
            return state.journey_status
        if not state.access_token:
            return state.journey_status
        info = await self._identity.me(access_token=state.access_token)
        raw_status: Any = None
        if isinstance(info, dict):
            user = info.get("user")
            if isinstance(user, dict):
                raw_status = user.get("journeyStatus") or user.get("journey_status")
            raw_status = raw_status or info.get("journeyStatus") or info.get("journey_status")
        if not isinstance(raw_status, str):
            return state.journey_status
        try:
            return JourneyStatus(raw_status)
        except ValueError:
            return state.journey_status

    @staticmethod
    def _parse_name(reply: Any) -> tuple[str, str]:
        if isinstance(reply, dict):
            first = reply.get("first_name")
            last = reply.get("last_name")
            if isinstance(first, str) or isinstance(last, str):
                return str(first or ""), str(last or "")
            text = str(reply.get("text") or "")
        else:
            text = reply_text(reply)
        parts = text.strip().split(maxsplit=1)
        if len(parts) == 2:
            return parts[0], parts[1]
        if parts:
            return parts[0], ""
        return "", ""

    async def _contextual_off_script(
        self,
        ctx: WorkflowContext,
        state: OnboardingState,
        reply: Any,
        *,
        default_answer: str,
    ) -> None:
        """Bug #7+#8 (2026-06-09): route off-script chat by intent BEFORE
        falling back to the LLM/canned line.

        QA reported every question while parked at prequal_wait /
        payment_wait got the SAME generic reply (the OpenAI key was 401-ing
        so the canned fallback fired every time). The wait nodes already
        own typed answers (``_safe_status_answer`` / ``_safe_portal_answer``
        / off-script help templates) — wire them in first so a status
        question gets a status answer even with the LLM offline."""

        help_template = _off_script_template(reply)
        if help_template is not None:
            await self._send(
                ctx,
                state,
                "onboarding.help.contextual",
                {
                    "answer": self._answer_for(help_template),
                    "next_step": _next_step_hint(state),
                },
            )
            return
        if _is_portal_query(reply):
            await self._send(
                ctx,
                state,
                "onboarding.help.contextual",
                {
                    "answer": await self._safe_portal_answer(state),
                    "next_step": _next_step_hint(state),
                },
            )
            return
        if _is_status_query(reply):
            await self._send(
                ctx,
                state,
                "onboarding.help.contextual",
                {
                    "answer": await self._safe_status_answer(state),
                    "next_step": _next_step_hint(state),
                },
            )
            return
        # Fall through to the smart/LLM path with the wait-node-specific
        # canned line; preserves the existing UX when intent isn't typed.
        await self._smart_contextual(ctx, state, reply, default_answer)

    async def _smart_contextual(
        self,
        ctx: WorkflowContext,
        state: OnboardingState,
        reply: Any,
        fallback_answer: str,
    ) -> None:
        """Answer an off-script message in context and re-state the current
        step gently. Uses the OpenAI model when available (so the agent
        actually understands questions like "why is CR needed?"), otherwise a
        canned line — never the robotic "text alone is not enough" nag."""

        hint = _next_step_hint(state)
        answer = await _llm_answer(reply_text(reply), hint)
        if answer:
            # The model answers ONLY the question (told not to invent/restate
            # steps); WE append the deterministic current-step nudge so guidance
            # never deviates from the real flow (user 2026-06-14).
            next_step = hint
        else:
            answer = fallback_answer
            next_step = hint
        await self._send(
            ctx,
            state,
            "onboarding.help.contextual",
            {"answer": answer, "next_step": next_step},
        )

    async def _send(
        self,
        ctx: WorkflowContext,
        state: OnboardingState,
        template_key: str,
        variables: dict[str, Any] | None = None,
        *,
        locale: str | None = None,
    ) -> None:
        await self._msg.send(
            channel=_channel(ctx),
            identity=ctx.identity,
            template_key=template_key,
            variables=variables or {},
            locale=locale or state.locale,
        )

    async def _update_progress(
        self,
        state: OnboardingState,
        ctx: WorkflowContext,
        step: int,
    ) -> int | None:
        """Send the canonical conversational step to Madad backend.

        Per Ishan (2026-06-07): WhatsApp leads must have their conversational
        step tracked via ``madad_mcp_update_onboarding_progress``. The backend
        hard-gates the pre-qualified document checklist on ``step >= 3``.

        Skips when:
          * the channel isn't WhatsApp (Email leads aren't tracked this way);
          * the step is <= the last recorded step (don't regress on retries);
          * the backend call fails (logged as warning, workflow continues).

        Returns the recorded step on success (caller writes it back to state)
        or None when skipped.
        """

        if ctx.channel is not Channel.WHATSAPP:
            return None
        if state.onboarding_progress_step is not None and step <= state.onboarding_progress_step:
            return None
        try:
            await self._identity.update_onboarding_progress(
                user_id=state.madad_user_id,
                channel=ctx.channel,
                identifier=ctx.identity,
                step=step,
                touch_inbound=False,
            )
            return step
        except Exception as exc:  # noqa: BLE001 — degrade in staging
            ctx.logger.warning(
                "update_onboarding_progress.failed",
                step=step,
                error=str(exc)[:200],
                note="staging-tolerant: continuing (will retry on next progression)",
            )
            return None

    @staticmethod
    def _step(name: str, ctx: WorkflowContext, **fields: Any) -> dict[str, Any]:
        entry = HistoryEntry(step=name, at=ctx.clock.now().isoformat())
        return {"history": [entry], **fields}


def _channel(ctx: WorkflowContext) -> Channel:
    assert ctx.channel is not None
    return ctx.channel


def _extract_status_source(payload: Any) -> str:
    """Pull ``last_status_source`` off a resume payload, defaulting to
    "poll". The dispatcher's :func:`translate_backend_event` stamps
    "webhook" on real Madad-backend events; explicit poller resumes (or
    test-driven manual resumes) get "poll"."""

    if isinstance(payload, dict):
        source = payload.get("last_status_source")
        if isinstance(source, str):
            return source
    return "poll"


def _extract_journey_status(payload: Any) -> JourneyStatus | None:
    """Pull ``journey_status`` off a resume payload and coerce to enum.

    The dispatcher's :data:`translate_backend_event` adds a status hint
    for well-known event types (eligibility.updated → PRE_QUALIFIED,
    offers.available → ACCEPTED, etc.). Returns ``None`` if the payload
    doesn't carry a recognisable status."""

    if not isinstance(payload, dict):
        return None
    raw = payload.get("journey_status")
    if not isinstance(raw, str):
        return None
    try:
        return JourneyStatus(raw)
    except ValueError:
        return None


def _extract_madad_score(payload: Any) -> int | None:
    """Pull a Madad score off a trigger payload. The score may sit at the top
    level or nested under ``payload`` (the backend webhook wraps it)."""

    if not isinstance(payload, dict):
        return None
    candidates: list[Any] = [payload.get("madadScore"), payload.get("madad_score")]
    inner = payload.get("payload")
    if isinstance(inner, dict):
        candidates += [inner.get("madadScore"), inner.get("madad_score")]
    for value in candidates:
        if isinstance(value, (int, float)):
            return int(value)
        if isinstance(value, str) and value.strip().isdigit():
            return int(value.strip())
    return None
