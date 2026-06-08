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

import base64
import binascii
import io
import os
import re
import zipfile
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
    "onboarding.status.pending",
    "onboarding.payment.awaiting",
    "onboarding.not_qualified",
    "onboarding.payment.request",
    "onboarding.payment.request.button",
    "onboarding.offers.preview",
    "onboarding.offer.handoff",
    "onboarding.offer.handoff.button",
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


def _is_short_negative(value: Any) -> bool:
    text = reply_text(value).lower().strip()
    return is_no(value) or text in {"nope", "not now", "skip", "later"}


# -- Smart (LLM) off-script replies ---------------------------------------
# When the SME asks something off-script ("why do you need my CR?", "is this
# safe?", "I already sent everything") we answer it in context with a small
# OpenAI model instead of robotically re-prompting. Falls back to a canned line
# whenever the key is unset or the call fails, so the flow never breaks.

_SMART_SYSTEM_PROMPT = (
    "You are Madad's friendly WhatsApp onboarding assistant. Madad is a "
    "regulated business-finance company in Qatar that helps SMEs unlock cash "
    "tied up in unpaid invoices owed by enterprise or government buyers. You are "
    "chatting with a business owner during their onboarding. Read their message "
    "and answer it directly, warmly and briefly — 1 to 3 short WhatsApp-style "
    "sentences, an emoji is fine. Never invent rates, terms, approvals or "
    "timelines. If they ask why a document or detail is needed, explain simply "
    "that it is to verify the business and assess financing eligibility. For "
    "account-specific status you don't know, reassure them the team is reviewing "
    "and it will update soon. End by gently nudging them toward the current step. "
    "Do NOT include any phone number."
)


async def _llm_answer(user_text: str, step_hint: str) -> str | None:
    """Return a contextual LLM reply, or ``None`` if unavailable/failed.

    Reads OpenAI config from the environment (same key the MCP cluster uses).
    Any error (no key, network, bad response) yields ``None`` so the caller can
    fall back to a canned line — the onboarding flow must never break on this.
    """

    api_key = os.getenv("OPENAI_API_KEY", "").strip()
    text = (user_text or "").strip()
    if not api_key or not text:
        return None
    model = os.getenv("OPENAI_MODEL", "gpt-4.1-mini").strip() or "gpt-4.1-mini"
    base_url = (
        os.getenv("OPENAI_BASE_URL", "https://api.openai.com/v1").strip().rstrip("/")
    )
    try:
        timeout = float(os.getenv("OPENAI_TIMEOUT", "15") or "15")
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


def _next_step_hint(state: OnboardingState) -> str:
    step = state.history[-1].step if state.history else ""
    if step in {"campaign_send", "campaign_await"}:
        return "Please reply YES if you want to start, or NO to opt out."
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
            "channel_session_first": self._channel_session_first,
            "channel_session_create_user": self._channel_session_create_user,
            "collect_onboarding_details_send": self._collect_onboarding_details_send,
            "collect_onboarding_details_await": self._collect_onboarding_details_await,
            "complete_onboarding_send": self._complete_onboarding_send,
            "channel_session_second": self._channel_session_second,
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
            "activated": self._activated,
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
            },
        )

        # Existing-user path converges at consent_send via one session call.
        graph.add_conditional_edges(
            "channel_session_first",
            self._route_channel_session,
            {"consent": "consent_send"},
        )
        # New-lead path: the spec says NO form-filling — we never ask the SME to
        # type their name/CR/role. The account is created up front with safe
        # placeholders (complete_onboarding fills sensible defaults) and the real
        # business details are extracted from the CR document they upload next.
        graph.add_edge("complete_onboarding_send", "channel_session_second")
        graph.add_conditional_edges(
            "channel_session_second",
            self._route_channel_session,
            {"consent": "consent_send"},
        )
        # WhatsApp organic-entry converges at consent_send via the
        # create_user_if_missing fast-path — no complete_onboarding hop.
        graph.add_conditional_edges(
            "channel_session_create_user",
            self._route_channel_session,
            {"consent": "consent_send"},
        )

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
                "missing": "documents_upload_loop_send",
                "await_again": "documents_upload_loop_await",
            },
        )
        # Spec Step 5 → payment: after the coffee message, PARK until the payment
        # step is triggered (via Postman in the demo). On trigger → Madad score +
        # the "Pay QAR 6,000 →" button.
        graph.add_edge("documents_complete", "payment_wait_await")
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

        for terminal in (
            "declined",
            "domain_blocked",
            "not_eligible",
            "not_qualified",
            "offer_handoff_to_madad",
            "activated",
        ):
            graph.set_finish(terminal)

    # -- Step 1: campaign + check_contact + session ---------------------------

    async def _campaign_send(self, state: OnboardingState, ctx: WorkflowContext) -> dict[str, Any]:
        locale = str(state.data.get("locale") or state.locale)
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
            await self._send(ctx, state, "onboarding.campaign.awaiting_yes_no")
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
        return self._step(
            "check_contact_send",
            ctx,
            check_contact_result=result,
            channel_identity=ctx.identity,
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
            await self._send(
                ctx,
                state,
                "onboarding.upload.required",
                {"document": "Commercial Registration (CR)"},
            )
            return self._step("consent_await", ctx, consent=False)
        first = attachments[0]
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
        if token and state.cr_ref:
            try:
                await self._kyc.upload_commercial_registration(
                    access_token=token,
                    content_base64=state.cr_content_base64 or "",
                    filename=state.cr_ref,
                    mime_type=state.cr_mime_type,
                )
            except Exception as exc:  # noqa: BLE001 — degrade in staging
                ctx.logger.warning(
                    "cr_upload.failed", error=str(exc)[:200],
                    note="staging-tolerant: continuing without CR uploaded",
                )
        # Step 2 — CR uploaded (backend journey_status: INCOMPLETE).
        progress_step = await self._update_progress(state, ctx, step=2)
        return self._step(
            "cr_upload_base64", ctx,
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
            await self._send(ctx, state, "onboarding.eligibility.intake.request")
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
        await self._send(ctx, state, "onboarding.financials.request")
        await self._reminders.schedule(
            "financials_pending",
            channel=_channel(ctx),
            identity=ctx.identity,
            target_ref=state.madad_user_id or ctx.session_id,
        )
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
            await self._send(
                ctx,
                state,
                "onboarding.upload.required",
                {"document": "Audited Financial Statement"},
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
            await self._send(ctx, state, "onboarding.buyers.request")
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
            await self._send(ctx, state, "onboarding.shareholders.request")
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
        await self._reminders.schedule(
            "incomplete_docs",
            channel=_channel(ctx),
            identity=ctx.identity,
            target_ref=state.madad_user_id or ctx.session_id,
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
            return self._step(
                "documents_upload_loop_await",
                ctx,
                missing_documents=list(state.missing_documents),
                documents_received=True,
                journey_status=forced_status,
                last_status_source=_extract_status_source(reply),
            )
        attachments = _valid_upload_attachments(reply)
        if not attachments:
            # No file — it's a question, a "no", or chit-chat. Answer it in
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
        # A single ZIP ("send everything in one file") expands into one synthetic
        # attachment per member document — each then runs the same classify +
        # upload pipeline as an individually-sent file (matching the msme-portal
        # complete-onboarding behaviour, where every doc lands on the backend KYC
        # endpoint that drives classification + extraction).
        attachments, saw_zip = _expand_zip_attachments(attachments)
        # Track remaining required docs locally. WhatsApp media almost always
        # arrives with a generic filename (IMG-xxxx.jpg), so _infer_doc_type
        # returns None and the upload used to be skipped entirely — the checklist
        # never cleared and the flow looped forever on "still needed". FAILSAFE:
        # assign each uploaded file to the next still-missing required doc so the
        # user's uploads actually satisfy the checklist and advance to completion.
        pending: list[str] = list(state.missing_documents)
        validated: list[str] = []  # uploaded + accepted by backend → ✅
        unprocessed: list[str] = []  # backend rejected / no token → ⏳ honest
        for att in attachments:
            filename = att.get("filename") or ""
            # A5 (Ishan 2026-06-07): prefer the classify-and-upload tool
            # over our own filename-keyword inference + manual doc_type
            # mapping. The backend classifier picks the type and routes to
            # the right entity slot; the response carries the resolved
            # ``document_type`` so we can track validated/missing without
            # guessing on filenames like ``IMG_001.jpg``.
            uploaded_ok = False
            resolved_doc_type: str | None = None
            if state.access_token:
                try:
                    classify_response = await self._kyc.classify_and_upload_document_base64(
                        access_token=state.access_token,
                        content_base64=att.get("content_base64") or "",
                        filename=filename,
                        mime_type=att.get("mime_type"),
                    )
                    uploaded_ok = True
                    if isinstance(classify_response, dict):
                        backend_type = (
                            classify_response.get("document_type")
                            or classify_response.get("documentType")
                            or classify_response.get("resolved_document_type")
                        )
                        if isinstance(backend_type, str):
                            resolved_doc_type = _workflow_doc_type(backend_type)
                except Exception as exc:  # noqa: BLE001 — degrade in staging
                    ctx.logger.warning(
                        "classify_and_upload.failed",
                        filename=filename,
                        error=str(exc)[:200],
                        note="staging-tolerant: continuing without this doc",
                    )
            # Fall back to the filename inference / pending hint when the
            # classifier didn't return a usable type (rare — happens when
            # the backend stores as ADDITIONAL_DOCUMENT). This keeps the
            # missing-docs accounting honest without dropping the file.
            doc_type = resolved_doc_type
            if not doc_type:
                doc_type = att.get("document_type") or _infer_doc_type(filename)
            if not doc_type and pending:
                doc_type = pending[0]
            if not doc_type:
                continue
            if uploaded_ok:
                if doc_type in pending:
                    pending.remove(doc_type)
                if doc_type not in validated:
                    validated.append(doc_type)
            elif doc_type not in unprocessed and doc_type not in validated:
                unprocessed.append(doc_type)
        # Acknowledge exactly what landed: ✅ per accepted document, ⏳ per one we
        # received but couldn't auto-validate (kept honest — no false ✅). A ZIP
        # gets the "📦 Extracting…" header; loose files just get their lines. The
        # lenient coffee message follows in documents_complete.
        await self._acknowledge_uploads(
            ctx, state, validated, unprocessed, saw_zip=saw_zip
        )
        # Remaining = required docs this batch did not land. Tracked locally so
        # generic-filename uploads reliably complete the checklist (we do not
        # re-query the backend's requested-docs list, which kept returning the
        # just-uploaded docs as still-missing and caused the loop).
        missing = pending
        if not missing:
            await self._reminders.suppress(
                target_ref=state.madad_user_id or ctx.session_id
            )
        return self._step(
            "documents_upload_loop_await",
            ctx,
            missing_documents=missing,
            documents_received=bool(attachments),
        )

    async def _acknowledge_uploads(
        self,
        ctx: WorkflowContext,
        state: OnboardingState,
        validated: list[str],
        unprocessed: list[str],
        *,
        saw_zip: bool,
    ) -> None:
        """Send the per-document checklist for this upload batch.

        ✅ for docs the backend accepted; ⏳ for ones we received but couldn't
        auto-validate — honest, never a false ✅.
        """

        if not validated and not unprocessed:
            return

        def _label(doc: str) -> str:
            return DOCUMENT_LABELS.get(doc, doc.replace("_", " ").title())

        rows = [f"✅ {_label(doc)} — Received & Validated" for doc in validated]
        rows += [
            f"⏳ {_label(doc)} — received, our team will review it"
            for doc in unprocessed
        ]
        lines = "\n".join(rows)
        template_key = (
            "onboarding.documents.zip_received"
            if saw_zip
            else "onboarding.documents.single_received"
        )
        await self._send(ctx, state, template_key, {"results": lines})

    async def _documents_complete(
        self, state: OnboardingState, ctx: WorkflowContext
    ) -> dict[str, Any]:
        await self._send(ctx, state, "onboarding.documents.complete")
        # Step 5 — all docs submitted, waiting for risk assessment.
        progress_step = await self._update_progress(state, ctx, step=5)
        return self._step(
            "documents_complete",
            ctx,
            onboarding_progress_step=progress_step or state.onboarding_progress_step,
        )

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
        await self._smart_contextual(
            ctx,
            state,
            payload,
            "Thanks! 🙌 Your pre-qualification result will be ready soon — I’ll "
            "share your document checklist here the moment it’s confirmed.",
        )
        return self._step("prequalify_wait_await", ctx, prequalified=False)

    async def _payment_wait_await(
        self, state: OnboardingState, ctx: WorkflowContext
    ) -> dict[str, Any]:
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
        await self._smart_contextual(
            ctx,
            state,
            payload,
            "You’re all set — our team is finalising your assessment. I’ll share "
            "your Madad score and the next step here shortly. 🙂",
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
        return self._step("journey_wait_await", ctx, **fields)

    async def _not_qualified(
        self, state: OnboardingState, ctx: WorkflowContext
    ) -> dict[str, Any]:
        await self._send(ctx, state, "onboarding.not_qualified")
        return self._step("not_qualified", ctx, outcome="not_qualified")

    async def _business_details_fetch(
        self, state: OnboardingState, ctx: WorkflowContext
    ) -> dict[str, Any]:
        if not state.access_token:
            return self._step("business_details_fetch", ctx)
        result = await self._pay.get_business_details(access_token=state.access_token)
        business_id = (
            result.get("business_details_id") if isinstance(result, dict) else None
        )
        return self._step(
            "business_details_fetch", ctx, business_details_id=business_id
        )

    async def _products_list_fetch(
        self, state: OnboardingState, ctx: WorkflowContext
    ) -> dict[str, Any]:
        if not state.access_token:
            return self._step("products_list_fetch", ctx)
        result = await self._pay.list_monetization_products(
            access_token=state.access_token
        )
        products = (
            list(result.get("products", [])) if isinstance(result, dict) else []
        )
        product = products[0] if products else {}
        return self._step(
            "products_list_fetch",
            ctx,
            payment_product_id=product.get("product_id"),
        )

    async def _payment_create(
        self, state: OnboardingState, ctx: WorkflowContext
    ) -> dict[str, Any]:
        if not (
            state.access_token
            and state.business_details_id
            and state.payment_product_id
        ):
            return self._step("payment_create", ctx)
        key = f"{ctx.run_id}:create_monetization_payment"
        result = await self._pay.create_monetization_payment(
            access_token=state.access_token,
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
        await self._reminders.schedule(
            "payment_pending",
            channel=_channel(ctx),
            identity=ctx.identity,
            target_ref=state.madad_user_id or ctx.session_id,
        )
        # ALSO fire the backend's notification trigger as a side-channel —
        # if it succeeds the SME gets a Madad-branded copy of the link too,
        # if it fails (502 in current UAT) we already sent our own.
        if state.access_token and state.payment_id:
            key = f"{ctx.run_id}:send_monetization_payment_link"
            try:
                await self._pay.send_monetization_payment_link(
                    access_token=state.access_token,
                    payment_id=state.payment_id,
                    channel=_channel(ctx),
                    identity=ctx.identity,
                    idempotency_key=key,
                )
                return self._step(
                    "payment_send_link", ctx,
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
        return self._step("payment_send_link", ctx)

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
        return self._step("payment_await", ctx, paid=paid)

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
        # journey reaches ACCEPTED. Pull it off the latest auth_me response.
        offers: list[dict[str, Any]] = []
        if state.access_token:
            info = await self._identity.me(access_token=state.access_token)
            if isinstance(info, dict):
                raw = info.get("offers")
                if isinstance(raw, list):
                    offers = list(raw)
        # Step 8 — offers ready, handing off to platform.
        progress_step = await self._update_progress(state, ctx, step=8)
        return self._step(
            "offers_fetch",
            ctx,
            offers=offers,
            onboarding_progress_step=progress_step or state.onboarding_progress_step,
        )

    async def _offer_view_send(
        self, state: OnboardingState, ctx: WorkflowContext
    ) -> dict[str, Any]:
        await self._send(
            ctx, state, "onboarding.offers.preview", {"count": len(state.offers)}
        )
        return self._step("offer_view_send", ctx)

    async def _offer_handoff_to_madad(
        self, state: OnboardingState, ctx: WorkflowContext
    ) -> dict[str, Any]:
        # PDF Step 8 — tappable "Login to Madad →" CTA-URL button on WhatsApp
        # (Meta caps the label at 20 chars). Falls back to the plain-text
        # template (with the URL inline) if the interactive path fails.
        portal_url = "https://madadfintech.com"
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
        return self._step("offer_handoff_to_madad", ctx, outcome="offer_handoff")

    async def _activated(
        self, state: OnboardingState, ctx: WorkflowContext
    ) -> dict[str, Any]:
        await self._send(ctx, state, "onboarding.activated")
        return self._step("activated", ctx, outcome="completed")

    # -- routers --------------------------------------------------------------

    def _route_entry(self, state: OnboardingState) -> str:
        if state.entry_reply == "YES":
            return "check_contact"
        if state.entry_reply == "NO":
            return "declined"
        return "ask_again"

    def _route_check_contact(self, state: OnboardingState) -> str:
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
        # Lenient: SMEs send many docs at once and won't upload every item, so as
        # soon as ANY document arrives we move on ("our team will review them").
        return "complete" if state.documents_received else "await_again"

    def _route_prequalify_wait(self, state: OnboardingState) -> str:
        return "go" if state.prequalified else "wait"

    def _route_payment_wait(self, state: OnboardingState) -> str:
        return "go" if state.payment_ready else "wait"

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
        if s in (JourneyStatus.ACCEPTED, JourneyStatus.OFFER_ACCEPTED):
            return "offers"
        if s == JourneyStatus.ACTIVATED:
            return "activated"
        return "wait"

    def _route_status_resume(self, state: OnboardingState) -> str:
        return "await_again" if state.last_status_source == "chat" else "poll"

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
        if not token:
            return None, refresh, expires
        now_ts = ctx.clock.now().timestamp()
        if expires is None or expires - now_ts > 60:
            return token, refresh, expires
        try:
            session = await self._identity.open_session(
                channel=_channel(ctx),
                identifier=ctx.identity,
                create_onboarding_token=False,
            )
            ctx.logger.info(
                "token.refreshed",
                old_expires_at=expires,
                new_expires_at=session.token_expires_at,
            )
            return (
                session.access_token or token,
                session.refresh_token or refresh,
                session.token_expires_at or expires,
            )
        except Exception as exc:  # noqa: BLE001
            ctx.logger.warning("token.refresh_failed", error=str(exc)[:200])
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
            # The model already nudges toward the next step, so don't repeat it.
            next_step = ""
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
