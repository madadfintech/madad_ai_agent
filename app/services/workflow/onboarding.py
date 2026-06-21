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
import hashlib
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
from app.services.document.checklist import ChecklistProvider
from app.shared.workflow.enums import Channel
from app.shared.workflow.state import HistoryEntry

from .mcp_kyc import workflow_doc_type as _workflow_doc_type
from .ports import (
    InMemoryInvoiceClient,
    InvoiceClient,
    KycClient,
    MadadIdentityClient,
    Messenger,
    MonetizationPaymentClient,
    Reminders,
)
from .state import JourneyStatus, OnboardingState, is_no, is_yes, reply_attachments, reply_text
from .webhook_dedupe import InMemoryWebhookDedupe, WebhookDedupe

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
    "onboarding.financials.received",
    "onboarding.documents.processing",
    "onboarding.documents.more_docs_prompt",
    "onboarding.documents.settle_prompt",
    "onboarding.documents.upload_failed",
    "onboarding.status.pending",
    "onboarding.payment.awaiting",
    "onboarding.payment.confirmed",
    "onboarding.qualified.waived",
    "onboarding.not_qualified",
    "onboarding.not_pre_qualified",
    "onboarding.not_qatar",
    "onboarding.payment.request",
    "onboarding.payment.request.button",
    "onboarding.offers.preview",
    "onboarding.offer.confirmed",
    "onboarding.activated",
    # Phase 1.b — invoice financing (post-activation) + repayment lifecycle.
    "onboarding.invoice.processing",
    "onboarding.invoice.submitting",
    "onboarding.invoice.received",
    "onboarding.invoice.failed",
    "onboarding.invoice.status",
    # UAT 2026-06-18 (Ishan Bug 1): SUBMIT-FIRST single-message ack — fires
    # right after the backend creates the invoice (instant) and before OCR
    # enrichment completes asynchronously.
    "onboarding.invoice.submitted",
    # UAT 2026-06-18 (Ishan QA): bulk SUBMIT-FIRST consolidated receipt —
    # replaces the old CSV preview + APPROVE ALL UX. Renders the count +
    # any failures inline so the SME never has to wonder which ones made it.
    "onboarding.invoice.bulk.submitted",
    # UAT 2026-06-19 QA #1: ONE consolidated "received N invoices —
    # processing" ack up front so the SME doesn't get per-file spam.
    "onboarding.invoice.bulk.processing",
    # UAT 2026-06-16 #3: single-PDF confirm card + Approve/Edit/Reject UX.
    "onboarding.invoice.confirm",
    "onboarding.invoice.edit.prompt",
    "onboarding.invoice.rejected",
    # UAT 2026-06-16 #4: bulk ZIP CSV preview + APPROVE ALL/EDIT/REMOVE.
    "onboarding.invoice.batch.preview",
    "onboarding.invoice.batch.csv_review",
    "onboarding.invoice.batch.help",
    "onboarding.invoice.batch.submitted",
    "onboarding.invoice.batch.cleared",
    "onboarding.disbursement.received",
    "onboarding.repayment.received",
    "onboarding.repayment.partially_paid",
    "onboarding.repayment.closed",
    "onboarding.repayment.due_soon",
    "onboarding.repayment.overdue",
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


_INVOICE_STATUS_PHRASES = (
    "my invoice", "my invoices", "invoice status", "invoices status",
    "status of my invoice", "where is my invoice", "what about my invoice",
    "invoice update", "where are my invoices", "list my invoices",
    "show my invoices", "show invoices", "any updates on my invoice",
    "fund", "funding", "disburs",  # "when will it disburse / fund"
)
_INVOICE_STATUS_SHORT_TOKENS = frozenset({"invoices?", "invoice?"})


def _is_invoice_status_query(value: Any) -> bool:
    """True when the SME is asking for the status of submitted invoices —
    used inside the post-activation ``_invoice_collect_await`` to dispatch
    the on-demand ``get_my_invoices`` read instead of the chit-chat fallback.
    """
    text = reply_text(value).strip().lower()
    if not text:
        return False
    if text in _INVOICE_STATUS_SHORT_TOKENS:
        return True
    return any(phrase in text for phrase in _INVOICE_STATUS_PHRASES)


# UAT 2026-06-16 (#9): structured Q&A intents the SME can ask at any
# time in invoice_collect_await. Each maps a phrase set to a backend
# read so we ground the answer in real data instead of LLM guesswork.
_QA_LIMIT_PHRASES = (
    "my limit", "credit limit", "approved limit", "available limit",
    "how much can i borrow", "how much can i finance",
    "what's my limit", "remaining limit",
)
_QA_DISBURSED_PHRASES = (
    "total disbursed", "how much disbursed", "disbursed so far",
    "total funded", "funded so far", "total funding",
)
_QA_DUE_PHRASES = (
    "what is due", "what's due", "due now", "due amount", "amount due",
    "what do i owe", "what do i need to pay", "outstanding",
    "next payment", "next due", "next emi", "upcoming payment",
)


class _QAIntent:
    """Tagged enum of supported self-service Q&A intents."""
    LIMIT = "limit"
    DISBURSED_TOTAL = "disbursed_total"
    DUE = "due"


def _qa_intent(value: Any) -> str | None:
    """Classify the SME's text into one of the self-service Q&A intents.
    Returns the intent tag or None if no Q&A intent matched."""
    text = reply_text(value).strip().lower()
    if not text:
        return None
    if any(p in text for p in _QA_LIMIT_PHRASES):
        return _QAIntent.LIMIT
    if any(p in text for p in _QA_DISBURSED_PHRASES):
        return _QAIntent.DISBURSED_TOTAL
    if any(p in text for p in _QA_DUE_PHRASES):
        return _QAIntent.DUE
    return None


_CONFIRM_APPROVE_TOKENS = frozenset({
    "approve", "approved", "ok", "okay", "confirm", "confirmed",
    "yes", "submit", "submit it", "go", "go ahead", "send", "all good",
    "looks good", "correct", "right",
    "invoice_approve",
})
_CONFIRM_REJECT_TOKENS = frozenset({
    "reject", "rejected", "cancel", "no", "discard", "delete", "stop",
    "remove", "drop",
    "invoice_reject",
})
_CONFIRM_EDIT_TOKENS = frozenset({
    "edit", "change", "modify", "update", "correction", "wrong",
    "fix", "fix it",
    "invoice_edit",
})


def _classify_confirm_action(value: Any) -> str | None:
    """Classify an invoice confirm reply (button tap or typed text) into
    ``approve`` / ``edit`` / ``reject`` or None when it doesn't fit.

    Buttons send the button title or id as the SME's text; we accept
    both. Edits with an explicit field/value ("edit amount: 32000")
    still classify as ``edit`` — the field/value parsing happens in
    a separate helper."""
    text = reply_text(value).strip().lower()
    if not text:
        return None
    # Check for compound edit ("edit amount: 5000") first.
    head = text.split(":", 1)[0].split()[0] if text else ""
    if head in _CONFIRM_EDIT_TOKENS:
        return "edit"
    if text in _CONFIRM_APPROVE_TOKENS:
        return "approve"
    if text in _CONFIRM_REJECT_TOKENS:
        return "reject"
    if text in _CONFIRM_EDIT_TOKENS:
        return "edit"
    return None


_EDIT_FIELD_ALIASES: dict[str, str] = {
    "amount": "total_amount",
    "total": "total_amount",
    "total amount": "total_amount",
    "totalamount": "total_amount",
    "amt": "total_amount",
    "date": "invoice_date",
    "invoice date": "invoice_date",
    "invoicedate": "invoice_date",
    "due": "due_date",
    "due date": "due_date",
    "duedate": "due_date",
    "invoice no": "invoice_number",
    "invoice number": "invoice_number",
    "no": "invoice_number",
    "number": "invoice_number",
    "inv": "invoice_number",
    "supplier": "supplier_name",
    "supplier name": "supplier_name",
    "buyer": "customer_name",
    "customer": "customer_name",
    "customer name": "customer_name",
}


def _parse_edit_field_value(text: str) -> tuple[str | None, str | None]:
    """Parse 'edit amount: 32000' / 'change buyer: ACME' style replies.

    Returns ``(field_canonical, new_value)`` when both parts are present,
    or ``(None, None)`` when the SME just tapped Edit with no field.
    """
    lowered = text.lower().strip()
    if ":" not in lowered:
        return None, None
    head, _, tail = lowered.partition(":")
    head = head.strip()
    # Strip the leading edit-verb token if present.
    head_tokens = head.split()
    if head_tokens and head_tokens[0] in _CONFIRM_EDIT_TOKENS:
        head = " ".join(head_tokens[1:]).strip()
    if not head:
        return None, None
    field = _EDIT_FIELD_ALIASES.get(head)
    if field is None:
        # Try matching individual words from the head.
        for word in head.split():
            if word in _EDIT_FIELD_ALIASES:
                field = _EDIT_FIELD_ALIASES[word]
                break
    if field is None:
        return None, None
    value = tail.strip()
    # Preserve original case for the value by re-slicing on text.
    raw_text = text.strip()
    if ":" in raw_text:
        value = raw_text.split(":", 1)[1].strip()
    if not value:
        return field, None
    return field, value


def _apply_invoice_edit(
    draft: dict[str, Any], field: str, value: str
) -> dict[str, Any]:
    """Return a fresh draft dict with ``field`` updated to ``value``.
    Numeric fields are coerced to int when the value parses cleanly."""
    updated = dict(draft)
    if field == "total_amount":
        try:
            updated[field] = int(float(value.replace(",", "")))
        except (TypeError, ValueError):
            updated[field] = value
    else:
        updated[field] = value
    return updated


_BATCH_APPROVE_PHRASES = (
    "approve all", "approveall", "submit all", "submit everything",
    "go", "go ahead", "confirm all", "ok all",
    "batch_approve_all",
)
_BATCH_EDIT_HEADS = frozenset({"edit", "change", "modify", "update"})
_BATCH_REMOVE_HEADS = frozenset({"remove", "delete", "drop", "discard"})
_BATCH_REJECT_PHRASES = (
    "reject all", "rejectall", "reject everything", "discard all",
    "cancel all", "decline all", "batch_reject_all",
)


def _classify_batch_action(value: Any) -> str | None:
    """Classify a reply at a pending batch into ``approve_all``,
    ``edit`` or ``remove``. Returns None when nothing matches."""
    text = reply_text(value).strip().lower()
    if not text:
        return None
    if any(p in text for p in _BATCH_REJECT_PHRASES):
        return "reject_all"
    if any(p in text for p in _BATCH_APPROVE_PHRASES):
        return "approve_all"
    head = text.split()[0] if text else ""
    if head in _BATCH_EDIT_HEADS:
        return "edit"
    if head in _BATCH_REMOVE_HEADS:
        return "remove"
    return None


_ROW_NUMBER_RE = re.compile(r"\b(\d+)\b")


def _parse_row_number(text: str) -> int | None:
    """Pull the first integer out of the SME's reply — the row number."""
    m = _ROW_NUMBER_RE.search(text or "")
    if not m:
        return None
    try:
        return int(m.group(1))
    except (TypeError, ValueError):
        return None


def _parse_batch_edit(
    text: str,
) -> tuple[int | None, str | None, str | None]:
    """Parse ``edit 2: amount 32000`` / ``edit 3: due 2026-07-28``.

    Returns ``(row, field_canonical, new_value)`` or all-None when the
    shape doesn't fit. The colon between row and field/value is the
    primary separator; field aliases come from ``_EDIT_FIELD_ALIASES``.
    """
    if ":" not in (text or ""):
        return None, None, None
    head, _, tail = text.partition(":")
    row = _parse_row_number(head)
    if row is None:
        return None, None, None
    tail = tail.strip()
    if not tail:
        return None, None, None
    # tail looks like "amount 32000" or "due 2026-07-28".
    tokens = tail.split(None, 1)
    if len(tokens) < 2:
        return None, None, None
    field_raw, value = tokens[0].lower(), tokens[1].strip()
    field = _EDIT_FIELD_ALIASES.get(field_raw)
    if field is None:
        return None, None, None
    return row, field, value


def _remove_batch_row(
    batch: list[dict[str, Any]], row: int,
) -> list[dict[str, Any]]:
    """Remove the row matching ``row`` from the batch + renumber the
    remaining rows so the SME's mental model stays 1-indexed."""
    out: list[dict[str, Any]] = []
    new_row = 1
    for entry in batch or []:
        if entry.get("row") == row:
            continue
        new_entry = dict(entry)
        new_entry["row"] = new_row
        out.append(new_entry)
        new_row += 1
    return out


def _apply_batch_edit(
    batch: list[dict[str, Any]], row: int, field: str, value: str,
) -> list[dict[str, Any]]:
    """Apply a field edit to one row, keeping the rest unchanged."""
    out: list[dict[str, Any]] = []
    for entry in batch or []:
        if entry.get("row") != row:
            out.append(entry)
            continue
        draft = entry.get("draft") or {}
        updated_draft = _apply_invoice_edit(draft, field, value)
        new_entry = dict(entry)
        new_entry["draft"] = updated_draft
        new_entry["flag"] = _flag_for_row(updated_draft)
        out.append(new_entry)
    return out


def _sum_batch_total(batch: list[dict[str, Any]]) -> tuple[int, str]:
    """Sum the per-row total_amount, returning (total, currency)."""
    total = 0
    currency = "QAR"
    for entry in batch or []:
        draft = entry.get("draft") or {}
        amt = draft.get("total_amount")
        try:
            total += int(float(amt)) if amt is not None else 0
        except (TypeError, ValueError):
            continue
        if isinstance(draft.get("currency"), str):
            currency = draft["currency"]
    return total, currency


def _flag_for_row(draft: dict[str, Any]) -> str:
    """Decide the per-row flag column. Marks rows whose extraction is
    obviously incomplete so the SME can review before approving."""
    missing: list[str] = []
    for key, label in (
        ("invoice_number", "ref"),
        ("supplier_name",  "supplier"),
        ("total_amount",   "amount"),
        ("due_date",       "due"),
    ):
        v = draft.get(key)
        if v in (None, "", "—"):
            missing.append(label)
    if missing:
        return "⚠️ " + "/".join(missing)
    # Low confidence flag from the extractor itself.
    conf = draft.get("confidently_extracted")
    if conf is False:
        return "⚠️ review"
    return "✅"


def _render_invoice_batch_table(
    batch: list[dict[str, Any]], currency: str, total: int,
) -> str:
    """Render a fixed-column table the SME can read on WhatsApp.

    Madad's spec asks for a CSV; until the cluster exposes a WhatsApp
    document-send tool, we render the same columns inline in a code
    block so the SME can scan it. The columns map 1:1 to the CSV
    Madad described."""
    lines = [
        "```",
        "Row  Invoice No        Date         Due          Customer            Amount       Flag",
    ]
    for entry in batch:
        draft = entry.get("draft") or {}
        row = str(entry.get("row") or "?").ljust(4)
        ref = str(draft.get("invoice_number") or "—")[:16].ljust(17)
        date = str(draft.get("invoice_date") or "—")[:12].ljust(13)
        due = str(draft.get("due_date") or "—")[:12].ljust(13)
        cust = str(draft.get("customer_name") or "—")[:19].ljust(20)
        amt = draft.get("total_amount")
        try:
            amt_s = f"{currency} {int(float(amt)):,}" if amt is not None else f"{currency} —"
        except (TypeError, ValueError):
            amt_s = f"{currency} {amt}"
        amt_s = amt_s[:12].ljust(13)
        flag = str(entry.get("flag") or "—")
        lines.append(f"{row} {ref} {date} {due} {cust} {amt_s} {flag}")
    lines.append("```")
    lines.append(f"\n📊 Batch total: {_fmt_qar(total, currency)}")
    return "\n".join(lines)


_CSV_COLUMNS = (
    "invoice_number", "invoice_date", "due_date",
    "customer_name", "supplier_name", "total_amount", "currency",
)
_CSV_HEADER_ALIASES = {
    "row": "row", "#": "row", "no.": "row", "sno": "row", "s.no": "row",
    "invoice_number": "invoice_number", "invoice no": "invoice_number",
    "invoice number": "invoice_number", "invoice": "invoice_number",
    "number": "invoice_number", "ref": "invoice_number", "no": "invoice_number",
    "invoice_date": "invoice_date", "date": "invoice_date", "invoice date": "invoice_date",
    "due_date": "due_date", "due": "due_date", "due date": "due_date",
    "customer_name": "customer_name", "customer": "customer_name",
    "buyer": "customer_name", "customer name": "customer_name",
    "supplier_name": "supplier_name", "supplier": "supplier_name",
    "supplier name": "supplier_name",
    "total_amount": "total_amount", "amount": "total_amount",
    "total": "total_amount", "total amount": "total_amount",
    "currency": "currency",
}


def _csv_cell(value: Any) -> str:
    if value in (None, "", "—"):
        return ""
    return str(value)


_BLOCK_LABEL_ALIASES = {
    "invoice_no": "invoice_number", "invoice no": "invoice_number",
    "invoice_number": "invoice_number", "invoice number": "invoice_number",
    "invoice": "invoice_number", "number": "invoice_number",
    "no": "invoice_number", "ref": "invoice_number",
    "date": "invoice_date", "invoice_date": "invoice_date", "invoice date": "invoice_date",
    "due": "due_date", "due_date": "due_date", "due date": "due_date",
    "buyer": "customer_name", "customer": "customer_name", "customer_name": "customer_name",
    "supplier": "supplier_name", "supplier_name": "supplier_name",
    "amount": "total_amount", "total": "total_amount", "total_amount": "total_amount",
    "currency": "currency",
}
_INVOICE_HDR_RE = re.compile(r"^\s*invoice\s+(\d+)\b", re.IGNORECASE | re.MULTILINE)


def _render_invoice_batch_csv(batch: list[dict[str, Any]], currency: str = "QAR") -> str:
    """Render the batch as an easy-to-edit labeled text sheet — one block per
    invoice. The SME edits the value after each ':' (keeping the labels) and
    sends the file back; ``_parse_invoice_batch_csv`` reads it back. Failed
    rows have blank values for the SME to fill in."""
    lines = [
        "MADAD — INVOICE REVIEW",
        "Edit the value after each ':'  (keep the labels). Leave blank if unknown.",
        "Then send this file back to submit — or tap Approve all in the chat.",
        "========================",
        "",
    ]
    for entry in batch or []:
        draft = entry.get("draft") or {}
        amount = draft.get("total_amount")
        try:
            amount_s = str(int(float(amount))) if amount not in (None, "", "—") else ""
        except (TypeError, ValueError):
            amount_s = _csv_cell(amount)
        lines += [
            f"Invoice {entry.get('row') or ''}".rstrip(),
            f"  invoice_no : {_csv_cell(draft.get('invoice_number'))}",
            f"  date       : {_csv_cell(draft.get('invoice_date'))}",
            f"  due        : {_csv_cell(draft.get('due_date'))}",
            f"  buyer      : {_csv_cell(draft.get('customer_name'))}",
            f"  supplier   : {_csv_cell(draft.get('supplier_name'))}",
            f"  amount     : {amount_s}",
            "------------------------",
        ]
    return "\n".join(lines) + "\n"


def _parse_invoice_batch_csv(text: str) -> list[dict[str, Any]]:
    """Parse the SME-edited review file back into per-invoice field dicts.

    Primary format is the labeled blocks (``Invoice N`` + ``label : value``);
    a comma-separated CSV is still accepted as a fallback."""
    out: list[dict[str, Any]] = []
    if not text or not text.strip():
        return out

    # Labeled-block format (what we now send).
    if _INVOICE_HDR_RE.search(text) is not None and ":" in text:
        cur: dict[str, Any] | None = None
        for line in text.splitlines():
            m = _INVOICE_HDR_RE.match(line)
            if m:
                if cur is not None:
                    out.append(cur)
                cur = {"row": int(m.group(1))}
                continue
            if cur is None or ":" not in line:
                continue
            label, _, value = line.partition(":")
            field = _BLOCK_LABEL_ALIASES.get(label.strip().lower())
            if field:
                cur[field] = value.strip()
        if cur is not None:
            out.append(cur)
        return [
            {"row": r.get("row"), **{f: r.get(f) for f in _CSV_COLUMNS}}
            for r in out
        ]

    # CSV fallback (legacy / if the SME pastes a spreadsheet export).
    import csv as _csv
    try:
        rows = [r for r in _csv.reader(io.StringIO(text)) if any((c or "").strip() for c in r)]
    except Exception:  # noqa: BLE001
        return out
    if not rows:
        return out
    header = [_CSV_HEADER_ALIASES.get((c or "").strip().lower(), (c or "").strip().lower()) for c in rows[0]]
    known = [h for h in header if h in ("row", *_CSV_COLUMNS)]
    if known:
        data_rows = rows[1:]
    else:
        header = ["row", *_CSV_COLUMNS]
        data_rows = rows
    for idx, raw in enumerate(data_rows, 1):
        rec: dict[str, Any] = {}
        for j, cell in enumerate(raw):
            if j < len(header) and header[j]:
                rec[header[j]] = (cell or "").strip()
        try:
            row_no = int(float(rec.get("row"))) if rec.get("row") else None
        except (TypeError, ValueError):
            row_no = None
        if row_no is None:
            row_no = idx
        out.append({"row": row_no, **{f: rec.get(f) for f in _CSV_COLUMNS}})
    return out


def _render_submitted_details(batch: list[dict[str, Any]]) -> str:
    """One labeled line per submitted invoice for the post-submit receipt.
    Returns '' (with no trailing blank lines) when there's nothing to show."""
    out: list[str] = []
    for entry in batch or []:
        draft = entry.get("draft") or {}
        out.append(
            f"📄 Invoice {entry.get('row')} — "
            f"Invoice No: {draft.get('invoice_number') or '—'}, "
            f"Buyer: {draft.get('customer_name') or '—'}, "
            f"Due: {draft.get('due_date') or '—'}"
        )
    return ("\n".join(out) + "\n\n") if out else ""


def _first_csv_attachment(value: Any) -> dict[str, Any] | None:
    """Return the first CSV/text attachment with bytes (the edited review
    sheet the SME sends back), else None."""
    for att in _valid_upload_attachments(value):
        mime = str(att.get("mime_type") or "").lower()
        name = str(att.get("filename") or "").lower()
        if (
            "csv" in mime
            or "text/plain" in mime
            or "application/csv" in mime
            or name.endswith((".csv", ".txt"))
        ):
            return att
    return None


def _draft_is_empty(draft: dict[str, Any]) -> bool:
    """True when the cluster's extract returned a draft with none of the
    fields the SME needs to confirm. Backend rejects empty submissions,
    so we'd rather ask for a resend than show an em-dash confirm card."""
    if not isinstance(draft, dict):
        return True
    significant_fields = (
        "invoice_number", "invoiceNumber",
        "total_amount", "totalAmount", "amount",
        "supplier_name", "supplierName",
        "customer_name", "customerName",
        "due_date", "dueDate",
        "invoice_date", "invoiceDate",
    )
    for key in significant_fields:
        value = draft.get(key)
        if value not in (None, "", "—", 0):
            return False
    return True


def _confirm_card_variables(draft: dict[str, Any]) -> dict[str, Any]:
    """Render the variables a confirm card template needs from a draft."""
    supplier = draft.get("supplier_name") or "—"
    invoice_number = draft.get("invoice_number") or "—"
    total = draft.get("total_amount")
    currency = draft.get("currency") or "QAR"
    due_date = draft.get("due_date") or "—"
    if total is None or total == "":
        amount_str = f"{currency} —"
    else:
        try:
            amount_str = f"{currency} {int(float(total)):,}"
        except (TypeError, ValueError):
            amount_str = f"{currency} {total}"
    buyer = draft.get("customer_name") or "—"
    summary = (
        f"📄 Extracted {invoice_number} · {amount_str} · Due {due_date}\n"
        f"Buyer: {buyer}"
    )
    return {
        "summary":   summary,
        "supplier":  supplier,
        "ref":       invoice_number,
        "amount":    amount_str,
        "due_date":  str(due_date),
        "customer":  str(draft.get("customer_name") or "—"),
    }


def _extract_credit_line(me_response: Any) -> dict[str, Any]:
    """Pull the active credit line dict off a /me response.

    Backends vary: ``user.creditLine`` (singular, most common) and
    ``user.creditLines[0]`` (legacy list). Returns the empty dict when
    nothing's there so callers can safely chain ``.get``."""
    if not isinstance(me_response, dict):
        return {}
    user = me_response.get("user") if isinstance(me_response.get("user"), dict) else me_response
    if not isinstance(user, dict):
        return {}
    cl = user.get("creditLine") or user.get("credit_line")
    if isinstance(cl, dict):
        return cl
    cls = user.get("creditLines") or user.get("credit_lines")
    if isinstance(cls, list):
        for entry in cls:
            if isinstance(entry, dict):
                return entry
    return {}


def _sum_disbursements(records: list[dict[str, Any]]) -> tuple[int, str]:
    """Sum the disbursement amounts on the agent's local ledger.
    Returns (total, currency). Defaults to QAR when the records don't
    have a currency tag."""
    total = 0
    currency = "QAR"
    for r in records:
        if not isinstance(r, dict):
            continue
        amt = r.get("amount")
        if isinstance(amt, (int, float)):
            total += int(amt)
        elif isinstance(amt, str):
            try:
                total += int(float(amt))
            except (TypeError, ValueError):
                continue
        if isinstance(r.get("currency"), str):
            currency = r["currency"]
    return total, currency


def _latest_emis_remaining(records: list[dict[str, Any]]) -> int | None:
    """Read the EMIs-remaining count from the most recent repayment
    record on state. Returns None when nothing is on file."""
    for r in reversed(records or []):
        if not isinstance(r, dict):
            continue
        v = r.get("emis_remaining")
        if isinstance(v, (int, float)):
            return int(v)
    return None


def _fmt_qar(value: Any, currency: str = "QAR") -> str:
    if value in (None, ""):
        return f"{currency} —"
    try:
        return f"{currency} {int(float(value)):,}"
    except (TypeError, ValueError):
        return f"{currency} {value}"


def _format_limit_answer(currency: str, limit: Any, available: Any) -> str:
    if limit is None and available is None:
        return (
            "I don't have your credit line details to hand right now. Please "
            "log in to uat-portal.madadfintech.com to see your approved "
            "limit, or call us on +974 3017 3888."
        )
    pieces: list[str] = []
    if limit is not None:
        pieces.append(f"Approved limit: {_fmt_qar(limit, currency)}")
    if available is not None:
        pieces.append(f"Available now: {_fmt_qar(available, currency)}")
    body = "\n".join(pieces)
    return (
        f"💳 {body}\n\n"
        "Send another invoice anytime and I'll submit it for financing."
    )


def _format_disbursed_answer(total: int, currency: str) -> str:
    if total <= 0:
        return (
            "I don't have any disbursement records yet on this conversation. "
            "Once a lender disburses an invoice you'll see the confirmation "
            "here and the figure will start tracking."
        )
    return (
        f"💸 Total disbursed so far: {_fmt_qar(total, currency)} "
        "(this number reflects what I've seen come through this chat). "
        "For the authoritative figure across all invoices, check "
        "uat-portal.madadfintech.com."
    )


def _format_due_answer(
    currency: str, outstanding: int | None, emis_remaining: int | None
) -> str:
    if outstanding is None and emis_remaining is None:
        return (
            "I don't have an outstanding balance recorded yet — once a "
            "repayment update arrives I'll have the number ready. You can "
            "also check uat-portal.madadfintech.com any time."
        )
    pieces: list[str] = []
    if outstanding is not None:
        pieces.append(f"Outstanding: {_fmt_qar(outstanding, currency)}")
    if emis_remaining is not None:
        word = "EMI" if emis_remaining == 1 else "EMIs"
        pieces.append(f"Remaining: {emis_remaining} {word}")
    body = " · ".join(pieces)
    return f"📅 {body}.\n\nReply here if anything needs clarification."


def _normalize_invoice_record(
    record: dict[str, Any], submitted_at_iso: str
) -> dict[str, Any]:
    """Flatten the backend's invoice shape into a fixed agent-side schema:
    ``{invoice_id, supplier_name, customer_name, total_amount, currency,
    invoice_number, status, submitted_at}``. Missing fields default to None
    or ``"—"`` so messages never render ``None`` as a value.
    """
    def _g(*keys: str) -> Any:
        for k in keys:
            v = record.get(k)
            if v not in (None, ""):
                return v
        return None

    return {
        "invoice_id": _g("invoice_id", "id"),
        "supplier_name": _g("supplier_name", "supplierName") or "—",
        "customer_name": _g("customer_name", "customerName") or "—",
        "total_amount": _g("total_amount", "totalAmount", "amount"),
        "currency": _g("currency") or "QAR",
        "invoice_number": _g("invoice_number", "invoiceNumber") or "—",
        "status": _g("status") or "SUBMITTED",
        "filename": _g("filename") or "invoice.pdf",
        "submitted_at": submitted_at_iso,
    }


def _format_accepted_invoices(invoices: list[dict[str, Any]]) -> str:
    """Render a per-invoice bullet list for the success receipt — one block
    per invoice with supplier / amount / reference."""
    lines: list[str] = []
    for idx, inv in enumerate(invoices, start=1):
        amount = inv.get("total_amount")
        currency = inv.get("currency") or "QAR"
        try:
            amount_str = f"{currency} {int(amount):,}" if amount is not None else f"{currency} —"
        except (TypeError, ValueError):
            amount_str = f"{currency} {amount}" if amount is not None else f"{currency} —"
        ref = inv.get("invoice_number") or "—"
        supplier = inv.get("supplier_name") or "—"
        lines.append(
            f"✅ Invoice {idx} — {supplier}\n"
            f"💰 {amount_str} · 📄 Ref: {ref}"
        )
    return "\n━━━━━━━━━━━━━\n".join(lines)


def _format_invoice_status_summary(invoices: list[dict[str, Any]]) -> str:
    """Render the SME's full invoice history with backend statuses for the
    on-demand status query."""
    if not invoices:
        return (
            "You haven't submitted any invoices yet — send one as a PDF or "
            "photo and I'll get it into financing for you. 🙂"
        )
    lines: list[str] = []
    for idx, inv in enumerate(invoices, start=1):
        if not isinstance(inv, dict):
            continue
        status = (
            inv.get("status")
            or inv.get("invoice_status")
            or inv.get("invoiceStatus")
            or "SUBMITTED"
        )
        # Friendly icon by status group.
        status_upper = str(status).upper()
        if status_upper in {"DISBURSED", "FUNDED", "PAID_OUT"}:
            icon = "💸"
        elif status_upper in {"REPAID", "CLOSED"}:
            icon = "✅"
        elif status_upper in {"REJECTED", "DECLINED"}:
            icon = "❌"
        elif status_upper in {"OVERDUE", "PAST_DUE"}:
            icon = "🚨"
        elif status_upper in {"DUE_SOON", "DUE"}:
            icon = "⏰"
        else:
            icon = "📄"
        supplier = (
            inv.get("supplier_name")
            or inv.get("supplierName")
            or "—"
        )
        ref = (
            inv.get("invoice_number")
            or inv.get("invoiceNumber")
            or "—"
        )
        amount = (
            inv.get("total_amount")
            or inv.get("totalAmount")
            or inv.get("amount")
        )
        currency = inv.get("currency") or "QAR"
        try:
            amount_str = f"{currency} {int(amount):,}" if amount is not None else f"{currency} —"
        except (TypeError, ValueError):
            amount_str = f"{currency} {amount}" if amount is not None else f"{currency} —"
        lines.append(f"{icon} Invoice {idx} — {supplier} · Ref {ref} · {amount_str} · {status}")
    return "\n".join(lines)


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
    "generally and say the exact figures will be shared with them here. If they "
    "ask why a document or detail is needed, explain simply that it verifies the "
    "business and assesses financing eligibility. For account-specific status you "
    "don't know, reassure them the team is reviewing and it will update soon.\n\n"
    "EVERYTHING HAPPENS HERE IN WHATSAPP: Do NOT tell the user to log in, visit a "
    "website/portal, or 'check your Madad account' — the entire application is "
    "handled right here in this chat. If they ask which documents are still "
    "needed or which they've already sent, answer from the document list provided "
    "in the step context below (do NOT redirect them elsewhere).\n\n"
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


_TRANSPORT_TIMEOUT_MARKERS = (
    "timed out",
    "timeout",
    "deadline exceeded",
    "connection closed",
    "connection reset",
    "stream closed",
    "transport closed",
    "remote disconnected",
)


def _looks_like_transport_timeout(exc: BaseException) -> bool:
    """True when the exception (or any link in its cause chain) looks like
    a transport-side timeout / disconnect rather than a backend "could not
    parse this file" error.

    Used to drive an honest invoice-failure message — when the MCP cluster
    timed out, the SME's file is fine and we should say so instead of
    asking them to resend.

    Prefers the typed ``MCPTimeoutError`` raised by the MCP client (the
    classifier handles asyncio.TimeoutError + transport-disconnect
    markers uniformly), falls back to the original string-scan for
    paths that haven't gone through ``call_tool`` (local-zip code,
    etc.)."""

    cur: BaseException | None = exc
    while cur is not None:
        from app.shared.mcp import MCPTimeoutError
        if isinstance(cur, MCPTimeoutError):
            return True
        # asyncio.TimeoutError is the canonical wait_for cancellation.
        if isinstance(cur, asyncio.TimeoutError | TimeoutError):
            return True
        msg = str(cur).lower()
        if any(marker in msg for marker in _TRANSPORT_TIMEOUT_MARKERS):
            return True
        cur = cur.__cause__ or cur.__context__
    return False


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
DOCS_PROCESSING_ACK_TTL_SECONDS = 120.0
# UAT 2026-06-18 (screenshot): Madad's bridge spaces ZIP-member POSTs
# across ~2 minutes for an 8-doc burst. With the old 30s window the
# SME saw the "Got it — please wait" ack THREE times in 90s. 120s
# covers the observed bridge tail; truly new upload sessions (5+ min
# later) still re-fire the ack as expected.
# UAT 2026-06-17: invoice attachment dedupe window. If the SAME content
# fingerprint arrives again within this window, treat it as a delivery
# retry (Meta/WhatsApp or our bridge fanning out) and silently drop —
# no second ack, no second extract call, no second failure message.
INVOICE_ATTEMPT_DEDUPE_SECONDS = 300.0
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


def _offer_date_key(offer: dict[str, Any]) -> str:
    """Sort key for offers — older-offered-first (user UAT 2026-06-14).

    Tries each timestamp field the backend has used (camelCase + snake_case).
    Returns an ISO-8601 string when present so direct lexicographic sort
    gives chronological order. Offers without any date sort to the end so a
    missing date can't shuffle the rest."""
    for k in (
        "createdAt", "created_at",
        "offeredAt", "offered_at",
        "createdDate", "created_date",
        "submittedAt", "submitted_at",
    ):
        v = offer.get(k)
        if isinstance(v, str) and v:
            return v
    # Past offers sort first; dateless offers go to the end ("￿" > any ISO).
    return "￿"


def _extract_offers_from_me(info: Any) -> list[dict[str, Any]]:
    """Read the offer list off an ``auth_me`` response.

    Per Ishan's handover §8 (2026-06-09): the field on the enriched
    ``/me`` payload is ``offersReceived``, not ``offers``. Earlier code
    only checked ``info.get("offers")`` and silently rendered empty
    offer cards in the WhatsApp "Exciting news — your financing offers
    are ready!" template (UAT screenshot 2026-06-10). Try every shape
    the backend is known to use so a one-off field rename can't drop
    offers on the floor again.

    Sorted chronologically per UAT 2026-06-14 — first-offered first.
    """

    if not isinstance(info, dict):
        return []
    for owner in (info, info.get("user") if isinstance(info.get("user"), dict) else None):
        if not isinstance(owner, dict):
            continue
        for key in ("offersReceived", "offers", "offer_list"):
            raw = owner.get(key)
            if isinstance(raw, list):
                return sorted(
                    (o for o in raw if isinstance(o, dict)),
                    key=_offer_date_key,
                )
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


def _invoice_attempt_sig(attachments: list[dict[str, Any]]) -> str:
    """Stable fingerprint of the attachment SET so a retried delivery is
    recognised without re-running extract. We hash a compact descriptor —
    filename + content-prefix per attachment — rather than full bytes, so
    a 5MB upload doesn't add 5MB to state on every receipt."""
    parts: list[str] = []
    for a in attachments or []:
        if not isinstance(a, dict):
            continue
        fn = (a.get("filename") or "").strip().lower()
        # Sample the first 4KB of the content — enough to make collisions
        # functionally impossible across distinct uploads while staying small.
        content = (a.get("content_base64") or "")[:4096]
        parts.append(f"{fn}|{content}")
    payload = "\n".join(sorted(parts))
    return hashlib.sha256(payload.encode("utf-8", errors="ignore")).hexdigest()[:32]


def _is_recent_invoice_retry(
    state: OnboardingState, sig: str, now: datetime
) -> bool:
    """True iff the same invoice fingerprint was just processed within the
    dedupe window. Survives a checkpoint replay so a poller-driven re-resume
    after the original send STILL recognises the duplicate."""
    if not sig or sig != (state.last_invoice_attempt_sig or ""):
        return False
    raw = state.last_invoice_attempt_at
    if not raw:
        return False
    try:
        prev = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return False
    return (now - prev).total_seconds() < INVOICE_ATTEMPT_DEDUPE_SECONDS


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
            (
                "processingFeeValue", "processing_fee_value",
                "processingFee", "processing_fee", "fee",
            ),
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
        # (user 2026-06-21) no canned "under review" line — it was wrongly
        # appended to every answer, incl. at the offer step. Stay silent here.
        return ""
    if step in {"invoice_collect_send", "invoice_collect_await"}:
        return ("Your credit line is ACTIVE — you can submit invoices for "
                "financing here anytime. Submitted invoices are reviewed by "
                "our team and you’ll get an update here once disbursed.")
    return "I’ll guide you step by step through the application."


def _is_conflict_error(exc: BaseException) -> bool:
    """True if any link in the exception's cause chain reports HTTP 409.

    The MCP client classifier promotes 409s to ``MCPConflictError``
    (preferred path); the legacy string-scan stays as a fallback so
    callers that pre-date the typed errors still work.
    """
    from app.shared.mcp import MCPConflictError
    cur: BaseException | None = exc
    while cur is not None:
        if isinstance(cur, MCPConflictError):
            return True
        cur = cur.__cause__ or cur.__context__
    # Fall through to the legacy string-scan for non-MCPError paths.
    seen: set[int] = set()
    cur = exc
    while cur is not None and id(cur) not in seen:
        seen.add(id(cur))
        message = str(cur)
        if "HTTP 409" in message or "status_code\":409" in message:
            return True
        cur = cur.__cause__ or cur.__context__
    return False


_DOCS_DONE_PHRASES = frozenset({
    "done", "all done", "im done", "i'm done", "that's all", "thats all",
    "that is all", "that's it", "thats it", "finished", "i'm finished",
    "im finished", "all sent", "sent all", "sent everything",
    "sent them all", "i've sent everything", "ive sent everything",
    "i sent everything", "everything sent", "no more", "no more docs",
    "no more documents", "nothing else", "nothing more", "all of them",
    "i have sent all", "already sent", "i already sent it", "i sent it already",
})


def _docs_button_intent(value: Any) -> str | None:
    """Map a tapped end-of-batch docs button (or its typed equivalent) to an
    intent: 'more' (upload more) or 'done' (proceed)."""
    t = reply_text(value).strip().lower().rstrip(" .!…")
    if not t:
        return None
    if t in (
        "yes, upload more", "yes upload more", "upload more",
        "yes, i want to upload more documents", "i want to upload more",
        "yes upload more documents",
    ):
        return "more"
    if t in (
        "no, i'm done", "no im done", "no, im done", "no i'm done",
        "no, i am done", "no i am done", "i'm done", "im done", "done",
    ):
        return "done"
    return None


def _looks_done_with_docs(value: Any) -> bool:
    """True when the SME signals they're finished uploading using natural
    phrasing (not just a literal NO) — used as the docs-loop escape hatch so a
    mis-classified-but-uploaded doc never traps them."""
    t = reply_text(value).strip().lower().rstrip(" .!…")
    if not t:
        return False
    return t in _DOCS_DONE_PHRASES or t.startswith((
        "done", "all done", "i'm done", "im done", "that's all", "thats all",
        "no more", "i've sent everything", "ive sent everything",
        "i sent everything", "i already sent",
    ))


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
        invoices: InvoiceClient | None = None,
        checklist: ChecklistProvider | None = None,
        dedupe: WebhookDedupe | None = None,
        offers_debounce_seconds: float = 30.0,
    ) -> None:
        self._msg = messenger
        self._identity = identity
        self._kyc = kyc
        self._pay = payments
        self._reminders = reminders
        # Offers-coalesce window. Madad fires one ``offers.available`` per
        # lender quote; two banks within 33 s drove the +919497191690
        # UAT bug (separate offer messages instead of one combined list).
        # Production default 30 s. The deps factory passes 0 in tests
        # because the deterministic test clock can't advance through the
        # debounce inside a single resume cycle.
        self._offers_debounce_seconds = max(0.0, offers_debounce_seconds)
        # Phase 1.b — invoice financing client. Optional only to keep
        # the long tail of existing test harnesses building without
        # naming a new ctor arg; the default in-memory fake fires when
        # the caller omits it, and ``build_onboarding_platform`` wires
        # the real ``McpInvoiceClient`` when ``mcp.enabled=True``.
        self._invoices: InvoiceClient = invoices or InMemoryInvoiceClient()
        # CMS-driven document checklist (M1 acceptance: "adding a doc to the
        # backend config must reflect in the next conversation"). None falls
        # back to ``DEFAULT_WHATSAPP_REQUIRED_DOCS`` so tests/dev keep working.
        self._checklist = checklist
        # Shared with the dispatcher (SET NX EX over Redis in prod). Used
        # for cross-execution inflight locks the LangGraph checkpoint can't
        # provide — specifically the slow invoice extract path where a
        # webhook retry races a still-running node. Optional: when None,
        # the InMemory fallback gives correct single-process behaviour.
        self._dedupe: WebhookDedupe = dedupe or InMemoryWebhookDedupe()

    # -- graph wiring ---------------------------------------------------------

    def build(self, graph: GraphBuilder) -> None:
        nodes: dict[str, Any] = {
            # Step 0: cold-start check_registration BEFORE we send the
            # campaign intro — returning users are detected on the first
            # inbound and routed into the RESUME chain instead of being
            # asked to sign up again.
            "entry_registration_check": self._entry_registration_check,
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
            "not_pre_qualified": self._not_pre_qualified,
            "not_qatar_based": self._not_qatar_based,
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
        # [TEMP-DBG] To find exact bug - Temp Logs:
        # Wrap every node with entry / exit / exception logging so the
        # workflow logs reveal the exact node where a request landed,
        # the next node it transitioned to, and any unexpected error.
        # Remove this block when the pipeline is stable.
        for node_name, fn in nodes.items():
            graph.add_node(node_name, self._dbg_wrap_node(node_name, fn))

        graph.set_entry("entry_registration_check")
        graph.add_conditional_edges(
            "entry_registration_check",
            self._route_entry_registration_check,
            {
                # Returning user: a route or registered payload came back —
                # mint a session and let the resume chain drop them at
                # the exact step their journey_status implies.
                "registered": "channel_session_resume",
                # No registration → normal campaign intro / SIGN_UP path.
                "fresh": "campaign_send",
            },
        )
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
            {
                "go": "documents_list_fetch",
                "wait": "prequalify_wait_await",
                # UAT 2026-06-17: admin marks SME as not pre-qualified
                # → fire the dedicated terminal instead of staying parked.
                "rejected": "not_pre_qualified",
            },
        )
        graph.add_edge("not_pre_qualified", "__end__")
        graph.add_edge("not_qatar_based", "__end__")
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
                # UAT 2026-06-16 (afternoon): waiver path skips even the
                # payment chain — fee is already settled, go straight
                # to the lender phase to await offers.
                "lender": "lender_status_poll",
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
            {
                "go":     "business_details_fetch",
                "wait":   "payment_wait_await",
                # UAT 2026-06-16 (afternoon): on a waiver the backend
                # advances the SME into the lender phase WITHOUT a real
                # payment ever being made — jump straight to the lender
                # poll so the payment chain (which would create + send a
                # TESS link the SME doesn't need) is skipped.
                "lender": "lender_status_poll",
            },
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
        # UAT 2026-06-16 nudge-spam RCA: a status poll / docs settle /
        # backend status_update webhook resuming at campaign_await
        # would fall into the else-branch below and fire its default
        # answer ("Are you interested in financing? Please reply YES
        # or NO.") via _contextual_off_script — every minute, forever,
        # while the run sat parked. Same guard as invoice_collect_await
        # uses for the same reason: re-park silently when the resume
        # is synthetic / non-SME and has no real text payload.
        if isinstance(reply, dict):
            reply_type = reply.get("type")
            if reply_type in {"status_update", "docs_settle", "phase1b_event"}:
                return self._step("campaign_await", ctx)
            if reply.get("last_status_source") in {"poll", "webhook"}:
                if not reply.get("text") and not reply.get("attachments"):
                    return self._step("campaign_await", ctx)
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

    async def _entry_registration_check(
        self, state: OnboardingState, ctx: WorkflowContext
    ) -> dict[str, Any]:
        """Cold-start ``check_registration`` BEFORE the campaign intro.

        Per Ishan (cluster commit ``e6ea5d2``, 2026-06-10): returning users
        should be detected on the FIRST inbound — before we send "Welcome
        to Madad — would you like to sign up?" to someone whose application
        is already in flight. The lookup is read-only and free, so we do
        it once at workflow entry. Three outcomes:

          * ``registered=True`` → store the route + payload on state and
            jump into the RESUME chain (channel_session_resume →
            resume_status_fetch → status-routed terminal/continue node).
          * ``registered=False`` → fall through to campaign_send (the
            original entry) so the normal SIGN_UP path runs unchanged.
          * Lookup error → log + fall through to campaign_send. The
            second check inside ``_check_contact_send`` (after YES)
            gives the SME another chance to be detected as returning.

        The follow-up check_registration call inside ``_check_contact_send``
        is guarded so it only re-fires when ENTRY's call returned no route
        (i.e. either ``registered=False`` or the request errored). This
        avoids a redundant round-trip on the returning-user path while
        keeping the original SIGN_UP detection working unchanged.
        """
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
                "entry_check_registration.failed", error=str(exc)[:200]
            )
        return self._step(
            "entry_registration_check",
            ctx,
            channel_identity=ctx.identity,
            registration_route=route,
            registration_payload=payload,
        )

    def _route_entry_registration_check(self, state: OnboardingState) -> str:
        """Branch from cold-start entry: registered → RESUME, else
        SIGN_UP / campaign intro."""
        decision = "registered" if state.registration_payload else "fresh"
        # [TEMP-DBG] To find exact bug - Temp Logs
        return self._dbg_route(
            "_route_entry_registration_check", state, decision,
            registration_route=state.registration_route,
            has_registration_payload=bool(state.registration_payload),
        )

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
        #
        # Guard: if the cold-start ``entry_registration_check`` node
        # already captured a route, skip this redundant call — the entry
        # node's lookup wins. We only re-check here when the entry call
        # returned no route (registered=False OR errored), in case the
        # SME's account state changed between entry and the YES reply.
        route: str | None = state.registration_route
        payload: dict[str, Any] = dict(state.registration_payload or {})
        if route is None and not payload:
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
        decision: str
        if s is None:
            decision = "welcome"
        elif s in (JS.SIGN_UP, JS.ONBOARDED):
            decision = "consent" if state.account_has_email else "email"
        elif s == JS.ELIGIBLE:
            decision = "financials"
        elif s in (JS.INCOMPLETE, JS.UNVERIFIED, JS.VERIFIED, JS.PRE_QUALIFIED):
            decision = "documents"
        elif s == JS.QUALIFIED:
            decision = "payment"
        elif s == JS.ACCEPTED:
            decision = "offers"
        elif s == JS.OFFER_ACCEPTED:
            decision = "offer_confirmed"
        elif s == JS.OFFER_EXPIRED:
            decision = "offer_expired"
        elif s == JS.ACTIVATED:
            decision = "activated"
        elif s == JS.NOT_ACCEPTED:
            decision = "rejected"
        elif s == JS.OPEN:
            decision = "application_open"
        elif s == JS.IN_ELIGIBLE:
            decision = "ineligible"
        elif s == JS.UNQUALIFIED:
            decision = "unqualified"
        else:
            decision = "welcome"
        # [TEMP-DBG] To find exact bug - Temp Logs
        return self._dbg_route(
            "_route_resume_by_status", state, decision,
            account_has_email=state.account_has_email,
        )

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
        # The graph routes "new" → complete_onboarding_send DIRECTLY without
        # going through _collect_onboarding_details_send (the only other site
        # that mints the onboarding_token). If state.onboarding_token is
        # absent, opening a session here mints one so the complete_onboarding
        # call below has a valid bearer — without it, Madad returns 401 and
        # this node degrades silently every time.
        onboarding_token = state.onboarding_token
        if not onboarding_token:
            try:
                bridge = await self._identity.open_session(
                    channel=_channel(ctx),
                    identifier=ctx.identity,
                    create_onboarding_token=True,
                )
                onboarding_token = bridge.onboarding_token
            except Exception as exc:  # noqa: BLE001 — degrade in staging
                ctx.logger.warning(
                    "complete_onboarding.token_mint_failed",
                    error=str(exc)[:200],
                )
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
                onboarding_token=onboarding_token or "",
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
        return self._step(
            "complete_onboarding_send", ctx,
            onboarding_token=onboarding_token or state.onboarding_token,
        )

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
        # UAT 2026-06-16 nudge-spam RCA: same synthetic-resume guard so
        # an orphaned-run poll never re-fires the "send your email" nag.
        if isinstance(reply, dict):
            reply_type = reply.get("type")
            if reply_type in {"status_update", "docs_settle", "phase1b_event"}:
                return self._step("business_email_await", ctx)
            if reply.get("last_status_source") in {"poll", "webhook"}:
                if not reply.get("text") and not reply.get("attachments"):
                    return self._step("business_email_await", ctx)
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
        # UAT 2026-06-16 nudge-spam RCA (Madad explicit ask): same
        # synthetic-resume guard as invoice_collect_await + campaign_await.
        # Without this, an orphaned-run poll OR a backend status_update
        # webhook landing here re-fires the canned CR/consent prompt.
        if isinstance(reply, dict):
            reply_type = reply.get("type")
            if reply_type in {"status_update", "docs_settle", "phase1b_event"}:
                return self._step("consent_await", ctx, consent=False)
            if reply.get("last_status_source") in {"poll", "webhook"}:
                if not reply.get("text") and not reply.get("attachments"):
                    return self._step("consent_await", ctx, consent=False)
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
        # doc ACTUALLY is, and ONLY claim "registered in Qatar — all good"
        # downstream when the classifier CONFIRMS it's a Commercial Registration.
        # Default FALSE (user 2026-06-14: uploading a LOGO still got the Qatar
        # line — because the old default was True and a logo classifies as
        # 'additional_document'/uncertain, which didn't flip it). Now: the
        # affirmation shows ONLY on a confident commercial_registration; a logo,
        # any other doc type, 'additional_document', or a classifier
        # timeout/error all leave it False → no false Qatar claim. A genuine CR
        # that fails to classify just misses the affirmation line (the financials
        # request still goes out) — far better than asserting Qatar for a non-CR.
        # UAT 2026-06-21: the affirmation "registered in Qatar — all good ✅"
        # was firing for ANYTHING (even a selfie) because the build set
        # cr_verified=True whenever the forced upload SUCCEEDED — which it does
        # for any file. Gate it on a FAST classify-ONLY check (the /classify
        # service — NO OCR extraction, so we never block 1-2 min just to decide
        # one line). cr_verified is True ONLY when the classifier confirms a
        # Commercial Registration; a logo / selfie / other → False → no false
        # Qatar claim. The actual CR upload stays the fast forced upload below
        # (lands it in the CR slot; backend extracts CR#/Qatar in background).
        cr_verified = False
        if token and state.cr_ref:
            try:
                cls = await self._kyc.classify_document_base64(
                    access_token=token,
                    content_base64=state.cr_content_base64 or "",
                    filename=state.cr_ref,
                    mime_type=state.cr_mime_type,
                )
                if isinstance(cls, dict):
                    backend_type = (
                        cls.get("document_type")
                        or cls.get("classification_label")
                        or ""
                    )
                    resolved = _workflow_doc_type(str(backend_type))
                    combined = f"{backend_type} {resolved}".lower()
                    cr_verified = (
                        "commercial_registration" in combined
                        and cls.get("classified") is not False
                    )
                    ctx.logger.info(
                        "cr_upload.classified",
                        backend_type=str(backend_type),
                        resolved=resolved,
                        cr_verified=cr_verified,
                    )
            except Exception as exc:  # noqa: BLE001 — gate degrades to no-affirmation
                ctx.logger.warning(
                    "cr_upload.classify_failed", error=str(exc)[:200],
                )
            # Forced CR upload (fast — lands the CR in the CR slot; backend
            # extracts CR#/Qatar validation in the background). Kept separate
            # from the classify so we never block on extraction.
            ctx.logger.info(
                "[TEMP-DBG] obs.kyc.upload",
                identity=ctx.identity,
                tool="upload_commercial_registration",
                filename=state.cr_ref,
                run_id=ctx.run_id,
                site="cr_upload",
            )
            try:
                await self._kyc.upload_commercial_registration(
                    access_token=token,
                    content_base64=state.cr_content_base64 or "",
                    filename=state.cr_ref,
                    mime_type=state.cr_mime_type,
                )
            except Exception as exc:  # noqa: BLE001 — degrade in staging
                ctx.logger.warning(
                    "cr_upload.forced_failed", error=str(exc)[:200],
                    note="forced CR upload failed; CR not recorded — surface to ops",
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
        # UAT 2026-06-16 (PM): immediate ack so the SME sees a response
        # before the cluster's upload + account-create roundtrip. The
        # financials → account.created chain takes a few seconds and the
        # silent gap matched the same pattern Bug #1 fixed for CR.
        try:
            await self._send(ctx, state, "onboarding.financials.received")
        except Exception as exc:  # noqa: BLE001 — ack failure must not kill the run
            ctx.logger.warning(
                "financials_received_ack.failed", error=str(exc)[:200],
            )
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
            # [TEMP-DBG] obs.kyc.upload
            ctx.logger.info(
                "[TEMP-DBG] obs.kyc.upload",
                identity=ctx.identity,
                tool="upload_audited_financial_report",
                filename=state.financials_filename or "audited_report.pdf",
                run_id=ctx.run_id,
                site="financials_upload",
            )
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
            cms_required: list[str] = []
            if self._checklist is not None:
                try:
                    items = await self._checklist.get_required(
                        "onboarding.whatsapp.required_docs"
                    )
                    cms_required = [item.code for item in items if item.required]
                except Exception as exc:
                    ctx.logger.warning(
                        "documents_list_fetch.cms_lookup_failed",
                        error=str(exc)[:200],
                    )
            missing = cms_required or list(DEFAULT_WHATSAPP_REQUIRED_DOCS)
            return self._step(
                "documents_list_fetch", ctx, missing_documents=missing
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
        # UAT 2026-06-21 (+919497191690 screenshot 11:34 AM): the
        # ``Your application has been pre-qualified!`` checklist landed
        # TWICE 167 ms apart — once from the workflow service handling
        # the prequalification.completed webhook, once from the celery
        # status_poller noticing the journey_status change at the same
        # tick. Both runners arrived at this node with
        # ``state.history`` still empty (their reads happened before
        # the other could checkpoint), so the ``already_asked`` check
        # below let both fire ``documents.checklist``.
        #
        # SET NX EX Redis claim per identity collapses the race: first
        # runner sends, second runner finds the lock and silently drops.
        # 120s TTL — long enough to swallow any in-flight retry, short
        # enough that a legitimate later loop-back can still re-fire
        # the (different) ``documents.missing`` reminder.
        checklist_key = f"docs:checklist:{ctx.identity}"
        try:
            first_runner = await self._dedupe.claim(
                checklist_key, ttl_seconds=120
            )
        except Exception as exc:  # noqa: BLE001 — Redis hiccup mustn't block onboarding
            ctx.logger.warning(
                "docs.checklist.claim_failed",
                error=str(exc)[:200],
                note="proceeding without inflight guard",
            )
            first_runner = True
        if not first_runner:
            ctx.logger.info(
                "docs.checklist.dedupe_skip", identity=ctx.identity,
                note="concurrent prequalified/poll runner already sent the checklist",
            )
            return self._step("documents_upload_loop_send", ctx)

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
        # UAT 2026-06-17 (RCA on +919497191690): ``qualified.waived`` can
        # land here if the admin waived the fee while the SME was still in
        # the docs loop. We previously advanced silently on the assumption
        # backend would send its own waiver message — it doesn't. Send the
        # waiver-qualified message ourselves so the SME doesn't stare at
        # the coffee message forever, then advance with paid=True.
        if (
            isinstance(reply, dict)
            and reply.get("type") == "payment"
            and bool(reply.get("paid"))
        ):
            await self._reminders.suppress(
                target_ref=state.madad_user_id or ctx.session_id
            )
            # UAT 2026-06-18 (Ishan): backend is the single owner of the
            # waiver message — it sends ONE consolidated "fee waived →
            # forwarded to banks" notice. Agent stays silent here and at
            # the other two waiver entry points (payment_wait_await,
            # payment_await) so the SME never gets a duplicate.
            progress_step = await self._update_progress(state, ctx, step=5)
            fields: dict[str, Any] = {
                "missing_documents": list(state.missing_documents),
                "documents_received": True,
                "paid": True,
                "payment_ready": True,
                "last_status_source": _extract_status_source(reply),
            }
            if progress_step is not None:
                fields["onboarding_progress_step"] = progress_step
            return self._step("documents_upload_loop_await", ctx, **fields)
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
            # UAT 2026-06-16 (afternoon, per Madad note): the post-
            # payment statuses (ACCEPTED+) imply the fee has been
            # settled by a real payment or a waiver. Mark paid=True so
            # the downstream ``_route_payment_wait`` (and route_payment
            # if a checkpoint replay brings us through payment_await)
            # jumps STRAIGHT to ``lender_status_poll`` instead of
            # running through ``business_details_fetch → payment_create
            # → payment_send_link`` and sending the SME a payment link
            # they don't need.
            fee_satisfied = forced_status in {
                JourneyStatus.ACCEPTED,
                JourneyStatus.OFFER_ACCEPTED,
                JourneyStatus.ACTIVATED,
            }
            score = _extract_madad_score(reply)
            fields = {
                "missing_documents": list(state.missing_documents),
                "documents_received": True,
                "journey_status": forced_status,
                "last_status_source": _extract_status_source(reply),
            }
            if fast_forward:
                fields["payment_ready"] = True
                if fee_satisfied:
                    fields["paid"] = True
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
        # End-of-upload settle (UAT 2026-06-14): the status-poller sweep
        # resumes this run with a synthetic ``docs_settle`` event after a
        # quiet window with no new uploads. Per user we no longer ASK the
        # SME "any more?" — but the flow must still advance silently or
        # the run gets stuck (UAT 2026-06-14: ZIP upload acked 8 ✅ + 3 ⏳,
        # then nothing). Auto-proceed when the SME has uploaded enough:
        # either every required slot has been touched (validated OR ⏳ acked)
        # OR the cumulative upload count has met the required threshold.
        # Fire the coffee message once for closure, set docs_proceed=True
        # so ``_route_documents`` advances to the next step.
        if _is_docs_settle(reply):
            # End-of-batch cue. The poller fires this ONLY when docs are still
            # missing, >=1 doc has been classified (docs_acked), the SME has
            # gone quiet (the whole batch is fully processed), and we haven't
            # prompted yet — so it can never fire mid-classification or twice.
            # Ask ONCE, with tappable buttons, whether they want to upload more
            # (user 2026-06-21). All-received never reaches here (it routes to
            # the "complete" path), so there are always pending docs to show.
            pending = list(state.missing_documents)
            if not pending:
                return self._step(
                    "documents_upload_loop_await", ctx,
                    docs_settle_prompted=True,
                    missing_documents=pending, documents_received=False,
                )
            # Structural duplicate-guard (user 2026-06-21): a settle resume for
            # a pending set we've ALREADY prompted for is inert — set the flag
            # and return WITHOUT sending. Only a genuine new upload (which
            # shrinks the pending set → a new signature) re-arms a real prompt.
            # This makes "exactly once per distinct pending set" structural, so
            # no timing race or duplicate resume can produce a second message.
            pending_sig = "|".join(sorted(pending))
            if state.more_docs_prompt_sig == pending_sig:
                ctx.logger.info(
                    "docs_more_prompt.dedupe_skip",
                    sig=pending_sig,
                    note="already prompted for this exact pending set",
                )
                return self._step(
                    "documents_upload_loop_await", ctx,
                    docs_settle_prompted=True,
                    missing_documents=pending, documents_received=False,
                )
            prompt_vars = {
                "documents": _format_documents(pending),
                "count": str(len(pending)),
            }
            sent_btn = False
            send_buttons = getattr(self._msg, "send_reply_buttons", None)
            if send_buttons is not None:
                try:
                    sent_btn = await send_buttons(
                        channel=_channel(ctx),
                        identity=ctx.identity,
                        template_key="onboarding.documents.more_docs_prompt",
                        buttons=[
                            ("docs_upload_more", "Yes, upload more"),
                            ("docs_done", "No, I'm done"),
                        ],
                        variables=prompt_vars,
                        locale=state.locale,
                    )
                except Exception as exc:  # noqa: BLE001
                    ctx.logger.warning(
                        "docs_more_prompt.buttons_failed", error=str(exc)[:200]
                    )
                    sent_btn = False
            if not sent_btn:
                try:
                    await self._send(
                        ctx, state, "onboarding.documents.more_docs_prompt",
                        prompt_vars,
                    )
                except Exception as exc:  # noqa: BLE001
                    ctx.logger.warning(
                        "docs_more_prompt.failed", error=str(exc)[:200]
                    )
            return self._step(
                "documents_upload_loop_await", ctx,
                docs_settle_prompted=True,
                more_docs_prompt_sig=pending_sig,
                missing_documents=pending, documents_received=False,
            )
        attachments = _valid_upload_attachments(reply)
        if not attachments:
            # End-of-batch button taps (user 2026-06-21): "Yes, upload more"
            # re-arms the prompt and waits for the next batch; "No, I'm done"
            # proceeds even with some docs still pending. (Tappable buttons —
            # no typed keywords required.)
            docs_btn = _docs_button_intent(reply)
            if docs_btn == "more":
                await self._send(
                    ctx, state, "onboarding.help.contextual",
                    {"answer": "Sure — send the rest whenever you're ready, as a "
                     "PDF, photo, or ZIP. \U0001f4ce", "next_step": ""},
                )
                return self._step(
                    "documents_upload_loop_await", ctx,
                    docs_settle_prompted=False,
                    missing_documents=list(state.missing_documents),
                    documents_received=False,
                )
            if docs_btn == "done":
                if not state.documents_complete_sent:
                    await self._send(ctx, state, "onboarding.documents.complete")
                return self._step(
                    "documents_upload_loop_await", ctx,
                    docs_proceed=True, documents_complete_sent=True,
                    missing_documents=list(state.missing_documents),
                    documents_received=False,
                )
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
            if (is_no(reply) or _looks_done_with_docs(reply)) and (
                state.more_docs_prompt_at or state.docs_uploaded_count > 0
            ):
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
        # UAT 2026-06-20 (post-457d271): cross-execution inflight guard,
        # same root cause as the invoice path. Madad's
        # ``classify_and_upload_document_base64`` takes several seconds
        # per file; the bridge / webhook caller can retry the inbound
        # before our node returns, spawning a concurrent run from the
        # same checkpoint. The state-level ack debounce below uses
        # ``state.documents_processing_ack_at`` which isn't visible to
        # concurrent runners (LangGraph writes state on node return only).
        # SET NX EX over Redis claims the (identity, attachment_sig) key
        # for 60s; the second runner sees the lock and silently drops
        # without re-sending ``documents.single_received`` or
        # re-uploading. Sig reuses _invoice_attempt_sig (filename +
        # 4KB-content fingerprint per attachment — same shape works).
        doc_sig = _invoice_attempt_sig(attachments)
        if doc_sig:
            inflight_key = f"docs:inflight:{ctx.identity}:{doc_sig}"
            try:
                first_runner = await self._dedupe.claim(
                    inflight_key, ttl_seconds=60
                )
            except Exception as exc:  # noqa: BLE001
                ctx.logger.warning(
                    "docs.inflight.claim_failed",
                    error=str(exc)[:200],
                    note="proceeding without inflight guard",
                )
                first_runner = True
            if not first_runner:
                ctx.logger.info(
                    "docs.inflight.dedupe_skip", sig=doc_sig,
                    note="concurrent doc upload already running for this set",
                )
                return self._step(
                    "documents_upload_loop_await", ctx,
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
        has_zip = any(_is_zip_attachment(a) for a in attachments)
        prior_ack = _parse_iso_or_none(state.documents_processing_ack_at)
        ack_age = (now - prior_ack).total_seconds() if prior_ack else None
        processing_ack_at: str | None = state.documents_processing_ack_at
        if ack_age is None or ack_age >= DOCS_PROCESSING_ACK_TTL_SECONDS:
            try:
                if has_zip:
                    # A ZIP is classified document-by-document server-side, which
                    # can take a while — set expectations so the SME doesn't think
                    # we went silent (user 2026-06-14). ZIP-only message.
                    await self._send(
                        ctx, state, "onboarding.documents.single_received",
                        {"results": (
                            "📦 Got your ZIP — classifying every document and "
                            "checking they're the right type. This can take up to "
                            "~5 minutes; I'll send the full checklist here as soon "
                            "as it's ready. ⏳"
                        )},
                    )
                else:
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
            # [TEMP-DBG] obs.kyc.upload
            ctx.logger.info(
                "[TEMP-DBG] obs.kyc.upload",
                identity=ctx.identity,
                tool="classify_and_upload_zip_base64",
                filename=att.get("filename") or "",
                run_id=ctx.run_id,
                site="docs_classify_zip",
            )
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
                    timeout=50.0,
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
            # [TEMP-DBG] obs.kyc.upload
            ctx.logger.info(
                "[TEMP-DBG] obs.kyc.upload",
                identity=ctx.identity,
                tool="classify_and_upload_document_base64",
                filename=filename,
                run_id=ctx.run_id,
                site="docs_classify_single",
            )
            try:
                classify_response = await asyncio.wait_for(
                    self._kyc.classify_and_upload_document_base64(
                        access_token=token,
                        content_base64=att.get("content_base64") or "",
                        filename=filename,
                        mime_type=att.get("mime_type"),
                    ),
                    timeout=50.0,
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
        # Per user 2026-06-14: no inline checklist + YES/NO prompt during
        # the upload phase. The brief per-upload receipt (single_received)
        # is the only ack the SME sees while uploading. Flow advances via
        # backend events (madad_score.ready / status → QUALIFIED+) or the
        # user's own NO escape hatch. docs_settle_prompted is left in the
        # state schema only because the settle-sweep early-return reads it.
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
            # A new upload = a new batch: re-arm so the poller re-prompts once
            # this batch settles (if anything is still missing).
            docs_settle_prompted=False,
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
        # UAT 2026-06-17: pre-qualification REJECTION (admin marks SME as
        # not pre-qualified) → route to the not-pre-qualified terminal so
        # the SME gets a clear next-steps message instead of silence.
        if isinstance(payload, dict) and payload.get("prequalification_rejected"):
            await self._reminders.suppress(
                target_ref=state.madad_user_id or ctx.session_id
            )
            return self._step(
                "prequalify_wait_await",
                ctx,
                prequalification_rejected=True,
            )
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
        if state.payment_ready or state.paid:
            return self._step("payment_wait_await", ctx)
        # PARK after the coffee message until the payment step is triggered
        # (Postman in the demo). Capture the Madad score from the trigger payload.
        payload = await_input({"waiting_for": "payment_ready", "step": "payment_wait"})
        # UAT 2026-06-18 (Ishan): ``qualified.waived`` lands here as
        # ``{type: payment, paid: True, event: qualified.waived}``.
        # Backend now owns the waiver message — it sends ONE consolidated
        # notice. Agent stays silent and just advances state into the
        # lender phase.
        if (
            isinstance(payload, dict)
            and payload.get("type") == "payment"
            and bool(payload.get("paid"))
        ):
            return self._step(
                "payment_wait_await", ctx,
                paid=True, payment_ready=True,
                last_status_source=_extract_status_source(payload),
            )
        # Same break-out for any post-payment journey_status hint
        # piggy-backed on a webhook (offers.available, offer.selected,
        # credit_line.activated). Mirrors the existing ``_payment_await``
        # short-circuit Ishaan added in 1bb9786.
        advanced = _extract_journey_status(payload)
        if advanced in (
            JourneyStatus.ACCEPTED,
            JourneyStatus.OFFER_ACCEPTED,
            JourneyStatus.ACTIVATED,
        ):
            return self._step(
                "payment_wait_await", ctx,
                paid=True, payment_ready=True,
                journey_status=advanced,
                last_status_source=_extract_status_source(payload),
            )
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
        # UAT 2026-06-16 (PM audit P0): a Phase 1.b webhook (disbursed
        # /repayment.*) misrouted here used to be silently re-parked,
        # losing the ledger update. Route it through the apply seam so
        # the SME-facing template still fires + state.disbursements /
        # repayments / outstanding still update, while staying at
        # journey_wait_await rather than forcing a node jump to
        # invoice_collect_await.
        if isinstance(payload, dict) and payload.get("type") == "phase1b_event":
            return await self._apply_phase1b_event_to_state(
                state, ctx, payload, target_step="journey_wait_await",
            )
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
            # Smart Groq answer (with real account state) instead of a canned
            # line, so questions like "difference between the tenures?" get a
            # real reply at the offer/wait step (user 2026-06-20).
            await self._smart_contextual(
                ctx, state, payload,
                "I’m here and tracking your application.",
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
        # Catch-all: a real SME free-text question we didn't pattern-match
        # (e.g. "what is my tenure?") → answer with Groq + live account/offer
        # state instead of silently re-parking. Synthetic poll/webhook/status
        # resumes carry no text, so they skip this and fall through to the
        # status-extraction below (user 2026-06-21: never go silent on a Q).
        if (
            _extract_journey_status(payload) is None
            and not (
                isinstance(payload, dict)
                and payload.get("type") in {"status_update", "docs_settle", "phase1b_event"}
            )
            and reply_text(payload).strip()
        ):
            await self._smart_contextual(
                ctx, state, payload,
                "I\u2019m here and tracking your application.",
            )
            return self._step("journey_wait_await", ctx, last_status_source="chat")
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

    async def _not_pre_qualified(
        self, state: OnboardingState, ctx: WorkflowContext
    ) -> dict[str, Any]:
        # UAT 2026-06-17 gap fix: the SME was parked at prequalify_wait_await
        # forever when the admin marked the business as not pre-qualified.
        # Dedicated terminal so they receive a clear next-steps message and
        # the run completes cleanly (vs a silent dead-end).
        await self._send(ctx, state, "onboarding.not_pre_qualified")
        return self._step(
            "not_pre_qualified", ctx, outcome="not_pre_qualified",
        )

    async def _not_qatar_based(
        self, state: OnboardingState, ctx: WorkflowContext
    ) -> dict[str, Any]:
        # UAT 2026-06-17 gap fix: terminal for SMEs whose CR shows the
        # business is registered outside Qatar. Madad's financing is
        # Qatar-only; we communicate that cleanly instead of dragging the
        # SME through financials + docs they can never use.
        await self._send(ctx, state, "onboarding.not_qatar")
        return self._step(
            "not_qatar_based", ctx, outcome="not_qatar_based",
        )

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
        # The backend may return either ``{"products": [...]}`` or a bare list
        # — tolerate both.
        if isinstance(result, dict):
            raw_products = result.get("products") or result.get("data") or []
        elif isinstance(result, list):
            raw_products = result
        else:
            raw_products = []
        products = [p for p in raw_products if isinstance(p, dict)]
        # CRITICAL BUG FIX (UAT 2026-06-15): the backend returns multiple
        # monetization products — "Onboarding charges" (6000, paid by
        # LENDER) and "Credit assessment charges" (3500, paid by SME). My
        # earlier code picked products[0] which was "Onboarding charges"
        # → SME was billed 6000 even though the SME pays the credit
        # assessment fee (3500). Filter to status=ACTIVE + chargedParty=SME
        # and prefer code=credit-assessment so the SME-facing payment
        # message and the create_monetization_payment call both use the
        # right product. Falls back to the first SME-paid active product
        # if the code changes, then to the first product overall to avoid
        # ever hard-failing on a backend rename.
        def _picked(p: dict[str, Any]) -> bool:
            return (
                str(p.get("status", "")).upper() == "ACTIVE"
                and str(p.get("chargedParty", "")).upper() == "SME"
            )
        sme_active = [p for p in products if _picked(p)]
        product = (
            next(
                (p for p in sme_active if str(p.get("code", "")).lower()
                 == "credit-assessment"),
                None,
            )
            or (sme_active[0] if sme_active else None)
            or (products[0] if products else {})
        )
        product_id = (
            product.get("product_id")
            or product.get("productId")
            or product.get("id")
        )
        # The backend's product field is ``amount`` (per UAT 2026-06-14 log);
        # snake/camel/other variants kept as defence-in-depth so a backend
        # rename can't put us back into the 6,000-fallback trap.
        amount_raw = (
            product.get("amount")
            or product.get("amount_qar")
            or product.get("amountQar")
            or product.get("value")
            or product.get("valueQar")
            or product.get("feeAmount")
            or product.get("priceAmount")
        )
        try:
            amount_qar: int | None = (
                int(amount_raw) if amount_raw is not None else None
            )
        except (TypeError, ValueError):
            amount_qar = None
        ctx.logger.info(
            "products_list_fetch.resolved",
            product_id=product_id,
            amount_qar=amount_qar,
            picked_name=product.get("name"),
            picked_code=product.get("code"),
            picked_charged_party=product.get("chargedParty"),
            total_products=len(products),
        )
        # [TEMP-DBG] To find exact bug - Temp Logs: dump every product the
        # backend returned so we can see WHY a particular one was picked.
        self._dbg(
            ctx, "products_list_fetch.full",
            total_products=len(products),
            products=[
                {
                    "id": p.get("product_id") or p.get("productId") or p.get("id"),
                    "code": p.get("code"),
                    "name": p.get("name"),
                    "status": p.get("status"),
                    "chargedParty": p.get("chargedParty"),
                    "amount": p.get("amount"),
                }
                for p in products
            ][:10],
        )
        return self._step(
            "products_list_fetch",
            ctx,
            payment_product_id=product_id,
            payment_amount_qar=amount_qar,
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
        # [TEMP-DBG] To find exact bug - Temp Logs
        self._dbg(
            ctx, "payment_create.request",
            business_details_id=state.business_details_id,
            product_id=state.payment_product_id,
            amount_qar_requested=state.payment_amount_qar or ONBOARDING_FEE_QAR,
            idempotency_key=key,
        )
        result = await self._pay.create_monetization_payment(
            access_token=token,
            business_details_id=state.business_details_id,
            product_id=state.payment_product_id,
            amount_qar=state.payment_amount_qar or ONBOARDING_FEE_QAR,
            idempotency_key=key,
        )
        # [TEMP-DBG]
        self._dbg(
            ctx, "payment_create.response",
            response_keys=sorted(result.keys()) if isinstance(result, dict) else None,
            payment_id=result.get("payment_id") or result.get("id")
            if isinstance(result, dict) else None,
            payable_amount=result.get("payableAmount") or result.get("payable_amount")
            if isinstance(result, dict) else None,
            has_payment_link=bool(isinstance(result, dict) and (
                result.get("paymentLink") or result.get("payment_link")
            )),
            internal_status=result.get("internalStatus") or result.get("status")
            if isinstance(result, dict) else None,
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
        # UAT 2026-06-18 (Ishan diagnosis on +919497191690): admin can set
        # a CUSTOM payable amount per application (e.g. 100 vs catalog
        # 3,500). Backend's CREATE response carries the AUTHORITATIVE
        # amount — read it and override the catalog-derived
        # state.payment_amount_qar so the WhatsApp "Pay QAR X" message
        # and Tess link both reflect the backend's actual decision.
        actual_amount: int | None = None
        if isinstance(result, dict):
            raw_amount = (
                result.get("payableAmount")
                or result.get("payable_amount")
                or result.get("amount")
                or result.get("amount_qar")
            )
            try:
                actual_amount = (
                    int(float(raw_amount)) if raw_amount is not None else None
                )
            except (TypeError, ValueError):
                actual_amount = None
        if actual_amount is not None and actual_amount > 0:
            ctx.logger.info(
                "payment_create.amount_resolved",
                catalog_amount=state.payment_amount_qar,
                actual_amount=actual_amount,
                note=(
                    "using backend's payableAmount (admin may have "
                    "overridden the catalog)"
                ),
            )
        return self._step(
            "payment_create",
            ctx,
            payment_id=payment_id,
            payment_status=payment_status,
            payment_link=payment_link,
            payment_provider_ref=provider_ref,
            payment_amount_qar=actual_amount or state.payment_amount_qar,
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
        amount = f"{(state.payment_amount_qar or ONBOARDING_FEE_QAR):,}"
        # Never fabricate a Madad score. When the backend has not sent a
        # real score, omit the entire score line (blank > hardcoded --
        # user 2026-06-19). This also drops the fixed "Strong" label,
        # which previously rendered regardless of the real score.
        score = state.madad_score
        score_line = (
            f"📊 Madad Score: {score}/100\n\n"
            "Based on this score, we believe you have high chances of "
            "getting approval from our banking partners. 💪\n\n"
            if score is not None else ""
        )
        variables = {
            "amount":         amount,
            "score":          score if score is not None else "",
            "score_line":     score_line,
            "payment_link":   state.payment_link or "",
            "provider_ref":   state.payment_provider_ref or "",
        }
        # [TEMP-DBG] To find exact bug - Temp Logs
        self._dbg(
            ctx, "payment_send_link.render",
            amount_rendered=amount,
            amount_qar_state=state.payment_amount_qar,
            amount_qar_default=ONBOARDING_FEE_QAR,
            score=score,
            has_payment_link=bool(state.payment_link),
        )
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
        # The PRIMARY payment link was already sent via our own messenger
        # above (CTA-URL button + plain-text fallback). We previously also
        # fired ``madad_payments_send_monetization_payment_link`` as a side
        # channel for a Madad-branded duplicate. UAT 2026-06-19: the call
        # returns HTTP 400 every time (recipient_phone payload-shape
        # rejected by the backend), generating constant monitor noise with
        # zero SME-visible benefit — the SME already has the link. Dropping
        # the side-channel call.
        token, refresh, expires = await self._live_token(state, ctx)
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
        # The qualification fee can be WAIVED by an admin: no payment.completed
        # ever reaches us — the backend sends its OWN consolidated "fee waived →
        # forwarded to banks" message and advances the journey straight to the
        # lender phase. A later offers.available / offer.selected /
        # credit_line.activated then lands HERE and used to be swallowed as
        # "unpaid" (so the lender offer card never rendered — UAT 2026-06-16).
        # If the resume carries a status already at/after the lender phase,
        # treat the fee as satisfied and break out into the lender flow. No
        # payment-confirmed message — the backend already messaged the waive.
        advanced = _extract_journey_status(result)
        if advanced in (
            JourneyStatus.ACCEPTED,
            JourneyStatus.OFFER_ACCEPTED,
            JourneyStatus.ACTIVATED,
        ):
            return self._step(
                "payment_await",
                ctx,
                paid=True,
                journey_status=advanced,
                last_status_source=_extract_status_source(result),
            )
        paid = bool(result.get("paid")) if isinstance(result, dict) else False
        # UAT 2026-06-16 (+918287611995): waiver detection on the POLL path.
        # Ishaan's commit 1bb9786 handles the webhook path — when a Phase 1
        # event (offers.available etc) lands at payment_await with the
        # journey_status piggy-backed on the resume payload, that
        # short-circuit fires first. But the background poller fires
        # ``{type: status_update, last_status_source: poll}`` with NO
        # journey_status, so Ishaan's hint check returns None and we'd
        # otherwise re-park forever for a silent admin waiver that fires
        # NO subsequent webhook at all. Read /me here as a defense-in-
        # depth: ``onboardingFeePaid=true`` OR a journey jump past
        # QUALIFIED both mean the fee is cleared. NO message is sent —
        # the backend's own consolidated "fee waived → forwarded to
        # banks" notice already reached the SME (same policy as
        # Ishaan's webhook branch above).
        waived = False
        if not paid and isinstance(result, dict) and result.get("type") in {
            "status_update", "phase1b_event",
        }:
            token = (await self._live_token(state, ctx))[0]
            if token:
                try:
                    info = await self._identity.me(access_token=token)
                except Exception as exc:  # noqa: BLE001 — fall through and stay parked
                    ctx.logger.warning(
                        "payment_await.me_failed", error=str(exc)[:200],
                    )
                else:
                    user = info.get("user") if isinstance(info, dict) else None
                    if isinstance(user, dict):
                        if bool(user.get("onboardingFeePaid") or user.get("onboarding_fee_paid")):
                            waived = True
                        raw_js = user.get("journeyStatus") or user.get("journey_status")
                        if isinstance(raw_js, str) and raw_js.upper() in {
                            "ACCEPTED", "OFFER_ACCEPTED", "ACTIVATED", "OPEN",
                        }:
                            waived = True
        if waived and not paid:
            # Mirror Ishaan's webhook short-circuit — advance silently into
            # the lender flow. last_status_source stays "poll" so the
            # poller suppresses its next tick for this run.
            return self._step(
                "payment_await", ctx,
                paid=True,
                last_status_source="poll",
            )
        # UAT 2026-06-18 (Ishan): ``qualified.waived`` comes in as
        # ``{type: payment, paid: True, event: qualified.waived}`` —
        # same paid=True signal as payment.completed but the SME hasn't
        # actually paid anything. Backend now owns the waiver message;
        # the agent stays silent (must NOT send onboarding.payment.confirmed
        # which would falsely say "Payment received!").
        event_marker = (
            str(result.get("event")) if isinstance(result, dict) else ""
        )
        if paid and event_marker == "qualified.waived":
            return self._step(
                "payment_await", ctx,
                paid=True,
                last_status_source="webhook",
            )
        if paid:
            await self._reminders.suppress(
                target_ref=state.madad_user_id or ctx.session_id
            )
            # UAT 2026-06-17 (+919497191690 screenshot 2:55 PM): the SME saw
            # TWO "🎉 Payment received" messages 1.3s apart — the status
            # poller re-entered ``payment_await`` while paid was already
            # True. Idempotency guard: only send the confirmed template
            # ONCE per run; later wakes still set state but stay silent.
            if state.payment_confirmed_sent:
                return self._step("payment_await", ctx, paid=paid)
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
                payment_confirmed_sent=True,
                application_ref=ref or state.application_ref,
            )
        return self._step("payment_await", ctx, paid=paid)

    async def _fetch_banks_to_send(
        self, state: OnboardingState, ctx: WorkflowContext
    ) -> list[str]:
        """Read ``BusinessDetails.banksToSend`` for the current SME — the list of
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
        # UAT 2026-06-16 (PM audit P0): same Phase 1.b safety net as
        # journey_wait_await. Backend should not normally fire a Phase
        # 1.b event at this stage, but if it does (delayed delivery,
        # race condition), update the ledger + render the SME-facing
        # template rather than silently dropping the data.
        if isinstance(payload, dict) and payload.get("type") == "phase1b_event":
            return await self._apply_phase1b_event_to_state(
                state, ctx, payload, target_step="lender_wait_await",
            )
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
            await self._smart_contextual(
                ctx, state, payload,
                "I’m here and tracking your lender review.",
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
        if (
            _extract_journey_status(payload) is None
            and not (
                isinstance(payload, dict)
                and payload.get("type") in {"status_update", "docs_settle", "phase1b_event"}
            )
            and reply_text(payload).strip()
        ):
            await self._smart_contextual(
                ctx, state, payload,
                "I\u2019m here and tracking your lender review.",
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
        # UAT 2026-06-17 (+919497191690 screenshot 9:26/9:27): the offer
        # cards re-fired 40s apart because ``offers_shown_sig`` was only
        # recorded by ``offer_handoff_to_madad`` AFTER the cards went out
        # — a second poll wake re-entered ``offer_view_send`` before sig
        # persisted, so the cards re-rendered. Use a SEPARATE sig field
        # for this node so its idempotency doesn't preempt the handoff
        # node's one-shot send.
        if _offers_sig(state.offers) == state.offers_preview_shown_sig:
            return self._step("offer_view_send", ctx)
        # UAT 2026-06-21 (+919497191690 screenshot 11:38/11:39 AM):
        # Madad emits one ``offers.available`` webhook PER lender that
        # quotes — two banks 33 seconds apart produced two separate
        # offer-preview messages (QIB alone, then QIB + CBoQ). The SME
        # only wanted the second cumulative message.
        #
        # 30-second debounce: on the first sighting record
        # ``offers_first_seen_at``. The send happens only after the
        # debounce window closes. The next driver here is either
        #   * the next ``offers.available`` webhook (33s later in this
        #     UAT — the debounce window expires and the cumulative
        #     set goes out), OR
        #   * the status_poller's 60-second tick for ACCEPTED-phase
        #     runs — guarantees the SME sees the offer even when only
        #     one lender ever quotes (single-offer case).
        # Picked 30s as the shortest debounce that catches the
        # "two banks within 33 s" race without delaying single-offer
        # SMEs beyond one poller tick.
        debounce_seconds = self._offers_debounce_seconds
        if debounce_seconds > 0:
            now = ctx.clock.now()
            first_seen = _parse_iso_or_none(state.offers_first_seen_at)
            if first_seen is None:
                # First time this offer set was observed — record + wait.
                return self._step(
                    "offer_view_send", ctx,
                    offers_first_seen_at=now.isoformat(),
                )
            if (now - first_seen).total_seconds() < debounce_seconds:
                # Still inside the coalesce window — let the next webhook
                # / poll re-enter this node when more offers may have arrived.
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
        return self._step(
            "offer_view_send", ctx,
            offers_preview_shown_sig=_offers_sig(state.offers),
        )

    async def _offer_handoff_to_madad(
        self, state: OnboardingState, ctx: WorkflowContext
    ) -> dict[str, Any]:
        # Skip the whole handoff block on a routine poll where the offer set is
        # unchanged (the run just passed through offer_view_send without
        # re-sending). Only (re)send the button when new offers were just shown.
        if _offers_sig(state.offers) == state.offers_shown_sig:
            return self._step("offer_handoff_to_madad", ctx, outcome="offer_handoff")
        # The offer cards (onboarding.offers.preview) are already sent by
        # offer_view_send. We deliberately DO NOT send the trailing
        # "please login to finalise your offer" handoff message any more
        # (user 2026-06-20: it replaced the offer in the SME's view and is
        # redundant). This node now only records the shown sig + advances.
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
        # Steps 10-13 (Phase 1.b): once the credit line is ACTIVATED the SME
        # can send invoices over WhatsApp/email. Routing:
        #   1. Phase 1.b webhook event (transaction.disbursed,
        #      repayment.received/.partially_paid/.closed/.due_soon/.overdue)
        #      → dispatch to the matching handler, send the SME-facing
        #      template, append to the local ledger.
        #   2. ``status`` query (e.g. "where are my invoices?") → call
        #      ``get_my_invoices`` and render the running status list.
        #   3. Attachments: ZIP → ``submit_zip_base64`` (server-side
        #      extract+submit per member). Single PDF/photo →
        #      ``extract_and_submit_base64`` (preferred path per Ishan's
        #      README §"Step 10: Invoice Submission" — one round-trip:
        #      backend extracts and submits in one shot).
        #   4. Empty chat → contextual answer, stay parked.
        reply = await_input({"waiting_for": "invoice", "step": "invoice_collect"})

        # Phase 1.b webhook events arrive marked by the dispatcher.
        if isinstance(reply, dict) and reply.get("type") == "phase1b_event":
            return await self._handle_phase1b_event(state, ctx, reply)

        # UAT 2026-06-16 (Bug #2, +918287611995): "Whenever you have an
        # invoice to finance, just send it here" fired 7 times in 6 min
        # because Phase 1.a webhooks (offer.selected, credit_line.activated,
        # transaction.disbursed echoes) AND the status poller / docs-settle
        # sweep AND any backend status_update legitimately resume this
        # parked-post-activation run. None of them are SME-side input, so
        # all of them used to fall through to _smart_contextual and re-fire
        # the canned prompt. Match journey_wait_await's pattern: re-park
        # silently when the resume is synthetic / non-SME.
        if isinstance(reply, dict):
            reply_type = reply.get("type")
            # Synthetic resumes — silently re-park, never message the SME.
            if reply_type in {"status_update", "docs_settle"}:
                return self._step("invoice_collect_await", ctx)
            if reply.get("last_status_source") in {"poll", "webhook"}:
                # A bare poll/webhook tick with no real SME content.
                if not reply.get("text") and not reply.get("attachments"):
                    return self._step("invoice_collect_await", ctx)

        # Self-service Q&A intents (UAT 2026-06-16 #9). Take precedence
        # over the broader status-query path because "what's my limit?"
        # could otherwise be misdetected as a status query and lose the
        # structured answer.
        intent = _qa_intent(reply)
        if intent is not None:
            return await self._handle_invoice_qa(state, ctx, intent)

        # Status-query path so the SME can ask "any update?" without
        # us misinterpreting it as chit-chat.
        if _is_invoice_status_query(reply):
            token, refresh, expires = await self._live_token(state, ctx)
            invoices: list[dict[str, Any]] = []
            if token:
                try:
                    info = await self._invoices.get_my_invoices(access_token=token)
                    invoices = list(info.get("invoices") or [])
                except Exception as exc:  # noqa: BLE001 — degrade in staging
                    ctx.logger.warning(
                        "invoice_status.fetch_failed", error=str(exc)[:200]
                    )
            await self._send(
                ctx, state, "onboarding.invoice.status",
                {
                    "summary": _format_invoice_status_summary(
                        invoices or state.invoices_submitted
                    ),
                    "count": len(invoices or state.invoices_submitted),
                },
            )
            return self._step(
                "invoice_collect_await", ctx,
                access_token=token, refresh_token=refresh, token_expires_at=expires,
            )

        # Approve / Edit / Reject reply from a pending confirm card
        # (UAT 2026-06-16 #3). The buttons send their title as the SME's
        # text reply via the WhatsApp interactive callback.
        if state.pending_invoice_draft is not None:
            confirm_action = _classify_confirm_action(reply)
            if confirm_action is not None:
                return await self._handle_invoice_confirm(
                    state, ctx, reply, confirm_action,
                )

        # Bulk CSV-preview controls (UAT 2026-06-16 #4): APPROVE ALL,
        # EDIT <row>: <change>, REMOVE <row>. Only when there's a
        # pending batch on state.
        if state.pending_invoice_batch:
            batch_action = _classify_batch_action(reply)
            if batch_action is not None:
                return await self._handle_invoice_batch_action(
                    state, ctx, reply, batch_action,
                )
            # An edited CSV file sent back at a pending batch → reconcile the
            # rows and submit (NOT a brand-new invoice upload).
            csv_att = _first_csv_attachment(reply)
            if csv_att is not None:
                try:
                    csv_text = base64.b64decode(
                        csv_att.get("content_base64") or ""
                    ).decode("utf-8", "replace")
                except Exception:  # noqa: BLE001
                    csv_text = ""
                return await self._handle_edited_csv(state, ctx, csv_text)

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

        # UAT 2026-06-17 (+919497191690 screenshot 2:58/2:59 PM): dedupe
        # a retried delivery of the SAME attachment(s). The bridge / Meta
        # sometimes fan one upload out as two inbounds; without this the
        # SME saw "Got your invoice…" + "We couldn't read the file" twice.
        sig = _invoice_attempt_sig(attachments)
        if _is_recent_invoice_retry(state, sig, ctx.clock.now()):
            ctx.logger.info(
                "invoice_attempt.deduped", sig=sig,
                note="silently dropping duplicate inbound within dedupe window",
            )
            return self._step("invoice_collect_await", ctx)

        # UAT 2026-06-19 QA #2: PERMANENT submitted-sig dedupe — refuse
        # to re-process anything we've already submitted in this run,
        # even after the 5min retry window expires. Status_poll / event
        # resumes (qualified.waived, offers.available, offer.selected,
        # credit_line.activated, status_poll_on_demand, transaction.*,
        # repayment.*) were re-entering ``_invoice_collect_await`` with
        # the same attachment payload from stale state, and 9 uploads
        # became 47 backend submits = 22+ portal rows. The set in state
        # is bounded to 200 sigs so it stays tiny.
        if sig and sig in (state.invoice_submitted_sigs or []):
            ctx.logger.info(
                "invoice.submitted_sig.dedupe_skip", sig=sig,
                note="run already submitted this attachment set; refusing re-submit",
            )
            # Never silently drop a re-upload — tell the SME it is already
            # in, so re-sending the same file never feels like dead silence.
            await self._smart_contextual(ctx, state, reply, 'You’ve already submitted this invoice and our team is reviewing it. 🙂 To finance another, just send a different invoice here.')
            return self._step("invoice_collect_await", ctx)

        # UAT 2026-06-19 QA: cross-execution inflight guard. Madad's
        # invoice extract takes 70-100s per file (their OCR latency, not
        # ours). The webhook caller (Meta bridge) times out at ~30s and
        # retries the inbound; each retry triggers a concurrent workflow
        # execution from the SAME checkpoint, so the state-level dedupe
        # above misses (LangGraph only writes state on node return). The
        # SME sees 3-4 ``bulk.processing`` acks and then "no response"
        # on subsequent uploads. SET NX EX over Redis: the first runner
        # claims the (identity, sig) key; concurrent retries see the
        # lock and silently drop.
        #
        # UAT 2026-06-21 (+919497191690 screenshot): the original 120 s
        # TTL was too tight. A 3-invoice bulk extract took 227 s; the
        # webhook retry came at +210 s, found the lock already expired,
        # ran its own extract for 166 s in parallel, and re-fired the
        # CSV preview AFTER the SME had already tapped APPROVE ALL on
        # the first runner's preview. Bumped to 600 s — comfortably
        # outlives the worst extract latency we've measured (235 s)
        # while still releasing within 10 minutes so a legitimate
        # later re-upload of the same set isn't blocked indefinitely.
        if sig:
            inflight_key = f"invoice:inflight:{ctx.identity}:{sig}"
            try:
                first_runner = await self._dedupe.claim(
                    inflight_key, ttl_seconds=600
                )
            except Exception as exc:  # noqa: BLE001 — Redis hiccup must not block uploads
                ctx.logger.warning(
                    "invoice.inflight.claim_failed",
                    error=str(exc)[:200],
                    note="proceeding without inflight guard",
                )
                first_runner = True
            if not first_runner:
                ctx.logger.info(
                    "invoice.inflight.dedupe_skip", sig=sig,
                    note="concurrent extract already running for this attachment set",
                )
                return self._step("invoice_collect_await", ctx)

        # Mint a live token from the verified identity (a long-active user can
        # resume with an empty/expired cached token — same fix as doc uploads).
        token, refresh, expires = await self._live_token(state, ctx)

        # UAT 2026-06-19 QA #3/#4: route to the EXTRACT-FIRST handlers
        # which check what OCR found and decide:
        #   * single + extract success + has fields → Approve/Edit card
        #   * single + extract fail / empty       → auto-submit blank
        #   * bulk   + extract success per row    → CSV preview + APPROVE ALL
        #   * bulk   + member extract fail        → auto-submit that one
        # See the docstrings on _invoice_extract_then_route_single /
        # _invoice_extract_then_route_bulk for the exact contract.
        non_zip = [a for a in attachments if not _is_zip_attachment(a)]
        has_zip = any(_is_zip_attachment(a) for a in attachments)
        if non_zip and not has_zip and len(non_zip) == 1:
            return await self._invoice_extract_then_route_single(
                state, ctx, non_zip[0], token, refresh, expires,
                attempt_sig=sig,
            )

        # UAT 2026-06-19 QA #4/#5: bulk takes the extract-first +
        # parallel + CSV review path. The submit-first legacy paths
        # remain below as dead code for one cycle, in case rollback is
        # needed. The legacy ``_invoice_bulk_preview`` is retained as dead
        # code for one cycle in case we need to roll back; it's no
        # longer reachable from this router.
        return await self._invoice_extract_then_route_bulk(
            state, ctx, attachments, token, refresh, expires,
            attempt_sig=sig,
        )

    async def _invoice_extract_then_route_single(
        self,
        state: OnboardingState,
        ctx: WorkflowContext,
        attachment: dict[str, Any],
        token: str | None,
        refresh: str | None,
        expires: int | None,
        *,
        attempt_sig: str | None = None,
    ) -> dict[str, Any]:
        """UAT 2026-06-19 QA #3 — extract-first conditional single-PDF flow.

        Per Madad PDF Step 10 + QA confirmation:
          * Extract succeeds with usable fields → render the Approve/Edit
            confirm card (SME can verify or correct OCR before submission).
          * Extract fails OR returns an empty draft → fall back to
            ``_invoice_submit_first`` (auto-submit blank; backend fills
            blank defaults and ops cleans up later).

        Only the "no file bytes" case still asks the SME to resend.
        """
        content = attachment.get("content_base64") or ""
        filename = attachment.get("filename") or "invoice.pdf"
        mime = attachment.get("mime_type")

        attempt_fields: dict[str, Any] = {}
        if attempt_sig:
            attempt_fields["last_invoice_attempt_sig"] = attempt_sig
            attempt_fields["last_invoice_attempt_at"] = (
                ctx.clock.now().isoformat()
            )

        if not content or not token:
            await self._send(
                ctx, state, "onboarding.invoice.failed",
                {"reason": "We didn't receive the file bytes — please resend."},
            )
            return self._step(
                "invoice_collect_await", ctx,
                access_token=token, refresh_token=refresh, token_expires_at=expires,
                **attempt_fields,
            )

        # Immediate ack — extract may take a few seconds.
        try:
            await self._send(ctx, state, "onboarding.invoice.processing")
        except Exception as exc:  # noqa: BLE001
            ctx.logger.warning(
                "invoice_processing_ack.failed", error=str(exc)[:200],
            )

        # [TEMP-DBG]
        self._dbg(
            ctx, "invoice.extract_then_route_single.request",
            filename=filename, content_b64_len=len(content),
        )

        try:
            draft = await self._invoices.extract_base64(
                access_token=token,
                filename=filename,
                content_base64=content,
                mime_type=mime,
            )
        except Exception as exc:  # noqa: BLE001 — fall through to auto-submit
            ctx.logger.warning(
                "invoice_extract.failed_falling_back_to_auto_submit",
                filename=filename, error=str(exc)[:200],
                error_type=type(exc).__name__,
            )
            return await self._invoice_submit_first(
                state, ctx, attachment, token, refresh, expires,
                attempt_sig=attempt_sig,
            )

        # [TEMP-DBG]
        self._dbg(
            ctx, "invoice.extract_then_route_single.draft",
            filename=filename,
            draft_keys=sorted(draft.keys()) if isinstance(draft, dict) else None,
            empty=_draft_is_empty(draft),
        )

        if _draft_is_empty(draft):
            # Extraction came back blank → auto-submit per QA: never
            # block the SME. Backend defaults fill in N/A.
            return await self._invoice_submit_first(
                state, ctx, attachment, token, refresh, expires,
                attempt_sig=attempt_sig,
            )

        # Extraction succeeded with usable fields → render the confirm
        # card and park awaiting Approve / Edit / Reject.
        await self._send_invoice_confirm_card(ctx, state, draft)
        return self._step(
            "invoice_collect_await", ctx,
            pending_invoice_draft=draft,
            pending_invoice_filename=filename,
            pending_invoice_content_b64=content,
            pending_invoice_mime=mime,
            pending_invoice_edit_field=None,
            access_token=token, refresh_token=refresh, token_expires_at=expires,
            **attempt_fields,
        )

    async def _invoice_extract_then_route_bulk(
        self,
        state: OnboardingState,
        ctx: WorkflowContext,
        attachments: list[dict[str, Any]],
        token: str | None,
        refresh: str | None,
        expires: int | None,
        *,
        attempt_sig: str | None = None,
    ) -> dict[str, Any]:
        """UAT 2026-06-19 QA #1+#4+#5 — bulk parallel extract + CSV review.

        Replaces the per-member submit spam (1 "reading it now" + 1
        "submitted ✅" per file = 20+ messages for 9 invoices) and the
        sequential ~90s-per-file extract bottleneck.

        Flow:
          1. ONE "📦 Received N invoices — processing" ack up front.
          2. Extract all members in PARALLEL (semaphore-capped at 5 to
             match the cluster's concurrency budget). 8 invoices land
             in ~10–15s instead of ~12 minutes.
          3. Triage each result:
             * Extract succeeded + has fields → goes to the CSV review
               batch (SME can Approve all / Edit / Remove).
             * Extract failed OR empty draft → auto-submit blank (ops
               fills it later). Counted in the receipt as "auto-submitted".
          4. If batch has reviewable rows → send the CSV preview + park
             awaiting APPROVE ALL / EDIT [row]: change / REMOVE [row].
          5. If all auto-submitted → send ONE consolidated receipt.
        """
        import asyncio as _asyncio

        attempt_fields: dict[str, Any] = {}
        if attempt_sig:
            attempt_fields["last_invoice_attempt_sig"] = attempt_sig
            attempt_fields["last_invoice_attempt_at"] = (
                ctx.clock.now().isoformat()
            )

        members, saw_zip = _expand_zip_attachments(attachments)
        # [TEMP-DBG]
        self._dbg(
            ctx, "invoice.bulk_extract_then_route.start",
            attachment_count=len(attachments),
            member_count=len(members),
            saw_zip=saw_zip,
        )
        if not members:
            await self._send(
                ctx, state, "onboarding.invoice.failed",
                {"reason": "We didn't see any invoice files in that batch. Please resend."},
            )
            return self._step(
                "invoice_collect_await", ctx,
                access_token=token, refresh_token=refresh, token_expires_at=expires,
                **attempt_fields,
            )

        # ONE consolidated processing ack — replaces the per-member
        # "reading it now" spam (QA #1).
        try:
            await self._send(
                ctx, state, "onboarding.invoice.bulk.processing",
                {"count": str(len(members))},
            )
        except Exception as exc:  # noqa: BLE001
            ctx.logger.warning(
                "invoice_bulk.processing_ack_failed", error=str(exc)[:200],
            )

        # Parallel extract — semaphore caps concurrency at 5 to match
        # the cluster's documented OCR concurrency=1 → 5 in-flight at
        # the agent gives the backend a healthy queue without
        # overwhelming it. Each extract has its own per-call timeout
        # (180s in mcp.provider) so the slowest member doesn't gate
        # the others.
        # OCR is single-worker (serializes); issuing N extracts in
        # parallel makes the later ones time out waiting in its queue
        # (UAT 2026-06-20 bulk failure). Extract ONE at a time so each
        # call gets a fresh per-call timeout. Slower but reliable.
        sem = _asyncio.Semaphore(1)

        async def _extract_one(
            idx: int, att: dict[str, Any],
        ) -> tuple[int, dict[str, Any], dict[str, Any] | None]:
            content = att.get("content_base64") or ""
            filename = att.get("filename") or f"invoice_{idx}.pdf"
            mime = att.get("mime_type")
            if not content or _is_zip_attachment(att):
                return idx, att, None
            async with sem:
                try:
                    return idx, att, await self._invoices.extract_base64(
                        access_token=token,
                        filename=filename,
                        content_base64=content,
                        mime_type=mime,
                    )
                except Exception as exc:  # noqa: BLE001
                    ctx.logger.warning(
                        "invoice_bulk.extract_failed",
                        idx=idx, filename=filename,
                        error=str(exc)[:200],
                        error_type=type(exc).__name__,
                    )
                    return idx, att, None

        triage = await _asyncio.gather(
            *(_extract_one(i, att) for i, att in enumerate(members, 1))
        )

        batch: list[dict[str, Any]] = []
        auto_submit_atts: list[dict[str, Any]] = []
        total_qar = 0
        currency = "QAR"
        row_num = 1
        for _idx, att, draft in triage:
            if isinstance(draft, dict) and not _draft_is_empty(draft):
                # Reviewable row — landed in the CSV preview.
                amount = draft.get("total_amount")
                try:
                    amount_int = int(float(amount)) if amount is not None else 0
                except (TypeError, ValueError):
                    amount_int = 0
                total_qar += amount_int
                if isinstance(draft.get("currency"), str):
                    currency = draft["currency"]
                batch.append({
                    "row":          row_num,
                    "draft":        draft,
                    "filename":     att.get("filename") or f"invoice_{row_num}.pdf",
                    "content_b64":  att.get("content_base64") or "",
                    "mime":         att.get("mime_type"),
                    "flag":         _flag_for_row(draft),
                })
                row_num += 1
            else:
                # Extraction failed or empty → auto-submit blank.
                auto_submit_atts.append(att)

        # SOME extracted, SOME failed → the failed uploads join the SAME
        # review CSV as EMPTY rows for the SME to fill in (user 2026-06-20),
        # rather than being auto-submitted blank behind their back. Only when
        # NOTHING extracted do we auto-submit the whole batch (Path B below).
        if batch:
            for att in auto_submit_atts:
                if not (att.get("content_base64") or ""):
                    continue
                batch.append({
                    "row":          row_num,
                    "draft":        {},
                    "filename":     att.get("filename") or f"invoice_{row_num}.pdf",
                    "content_b64":  att.get("content_base64") or "",
                    "mime":         att.get("mime_type"),
                    "flag":         _flag_for_row({}),
                })
                row_num += 1
            auto_submit_atts = []

        # Auto-submit the blanks in PARALLEL — they all share the same
        # backend create path so they bench like the extracts.
        async def _submit_blank(att: dict[str, Any]) -> dict[str, Any] | None:
            content = att.get("content_base64") or ""
            filename = att.get("filename") or "invoice.pdf"
            mime = att.get("mime_type")
            if not content or not token:
                return None
            async with sem:
                try:
                    response = await self._invoices.extract_and_submit_base64(
                        access_token=token,
                        filename=filename,
                        content_base64=content,
                        mime_type=mime,
                    )
                except Exception as exc:  # noqa: BLE001
                    ctx.logger.warning(
                        "invoice_bulk.auto_submit_failed",
                        filename=filename, error=str(exc)[:200],
                    )
                    return None
            invoice_id = (
                response.get("invoice_id") or response.get("id")
                if isinstance(response, dict) else None
            )
            return {
                "invoice_id": invoice_id,
                "filename":   filename,
                "submitted_at": ctx.clock.now().isoformat(),
                "status":     "SUBMITTED",
            }

        auto_results = (
            await _asyncio.gather(*(_submit_blank(a) for a in auto_submit_atts))
            if auto_submit_atts else []
        )
        auto_ledger = [r for r in auto_results if r is not None]
        auto_failed = len(auto_submit_atts) - len(auto_ledger)

        # Path A — reviewable CSV (one or more rows extracted OK; any failed
        # uploads ride along as EMPTY rows for the SME to fill). Send the batch
        # as a real CSV document + an APPROVE ALL prompt; fall back to the
        # inline table if the backend document route isn't live yet.
        if batch:
            csv_text = _render_invoice_batch_csv(batch, currency)
            sent_doc = await self._send_invoice_csv(
                ctx, state, csv_text,
                count=len(batch), total=total_qar, currency=currency,
            )
            if not sent_doc:
                table = _render_invoice_batch_table(batch, currency, total_qar)
                await self._send(
                    ctx, state, "onboarding.invoice.batch.preview",
                    {
                        "table":   table,
                        "count":   str(len(batch)),
                        "total":   _fmt_qar(total_qar, currency),
                        "saw_zip": "yes" if saw_zip else "no",
                        "failed":  str(auto_failed),
                    },
                )
            new_sigs = list(state.invoice_submitted_sigs or [])
            # Note: we DON'T record attempt_sig in submitted_sigs yet
            # — the SME hasn't approved the batch yet. The sig is
            # recorded when APPROVE ALL fires (handle_invoice_batch_action).
            return self._step(
                "invoice_collect_await", ctx,
                pending_invoice_batch=batch,
                pending_invoice_batch_total_qar=total_qar,
                pending_invoice_batch_currency=currency,
                invoices_submitted=[*state.invoices_submitted, *auto_ledger],
                invoice_submitted_sigs=new_sigs[-200:],
                access_token=token, refresh_token=refresh, token_expires_at=expires,
                **attempt_fields,
            )

        # Path B — everything auto-submitted (no extract succeeded) →
        # ONE consolidated receipt.
        if auto_ledger:
            failure_block = (
                f"\n\n⚠️ {auto_failed} didn't submit cleanly — please resend those."
                if auto_failed else ""
            )
            noun = "invoice" if len(auto_ledger) == 1 else "invoices"
            await self._send(
                ctx, state, "onboarding.invoice.bulk.submitted",
                {
                    "count": str(len(auto_ledger)),
                    "noun":  noun,
                    "details": "",
                    "failure_block": failure_block,
                },
            )
            new_sigs = list(state.invoice_submitted_sigs or [])
            if attempt_sig:
                new_sigs.append(attempt_sig)
            return self._step(
                "invoice_collect_await", ctx,
                invoices_submitted=[*state.invoices_submitted, *auto_ledger],
                invoice_submitted_sigs=new_sigs[-200:],
                access_token=token, refresh_token=refresh, token_expires_at=expires,
                **attempt_fields,
            )

        # Path C — nothing got through. Be honest, ask for a retry.
        await self._send(
            ctx, state, "onboarding.invoice.failed",
            {
                "reason": (
                    "We had a brief issue submitting your invoices — "
                    "please try once more in a minute."
                ),
            },
        )
        return self._step(
            "invoice_collect_await", ctx,
            access_token=token, refresh_token=refresh, token_expires_at=expires,
            **attempt_fields,
        )

    async def _invoice_submit_first(
        self,
        state: OnboardingState,
        ctx: WorkflowContext,
        attachment: dict[str, Any],
        token: str | None,
        refresh: str | None,
        expires: int | None,
        *,
        attempt_sig: str | None = None,
    ) -> dict[str, Any]:
        """UAT 2026-06-18 (Ishan Bug 1) — SUBMIT-FIRST single PDF flow.

        Backend's ``extract_and_submit_base64`` creates the invoice
        instantly (blank defaults that ops fills) and enriches via OCR
        in the background within ~5s. The agent must NOT block on
        synchronous OCR — that produced the "We couldn't read the file"
        failure on every multi-page invoice (scanned multi-page PDFs
        OCR in 90s+).

        Send the SME a simple "submitted ✅" ack. Backend's later webhook
        confirms the details once OCR completes; no resend / no confirm
        card / no Approve-Edit-Reject UX. Only a true "no file bytes"
        case still asks for a resend.
        """
        content = attachment.get("content_base64") or ""
        filename = attachment.get("filename") or "invoice.pdf"
        mime = attachment.get("mime_type")

        attempt_fields: dict[str, Any] = {}
        if attempt_sig:
            attempt_fields["last_invoice_attempt_sig"] = attempt_sig
            attempt_fields["last_invoice_attempt_at"] = (
                ctx.clock.now().isoformat()
            )

        if not content or not token:
            await self._send(
                ctx, state, "onboarding.invoice.failed",
                {"reason": "We didn't receive the file bytes — please resend."},
            )
            return self._step(
                "invoice_collect_await", ctx,
                access_token=token, refresh_token=refresh, token_expires_at=expires,
                **attempt_fields,
            )

        # [TEMP-DBG] To find exact bug - Temp Logs
        self._dbg(
            ctx, "invoice.submit_first.request",
            filename=filename,
            mime=mime,
            content_b64_len=len(content),
        )

        # Immediate ack — the SME sees acknowledgement before the (fast)
        # submit completes. Backend confirms via webhook later.
        try:
            await self._send(ctx, state, "onboarding.invoice.processing")
        except Exception as exc:  # noqa: BLE001 — never block submit on the ack
            ctx.logger.warning(
                "invoice_processing_ack.failed", error=str(exc)[:200],
            )

        # [TEMP-DBG] obs.invoice.submit — behavioral instrumentation. The
        # log monitor watches for ≥3 submits per identity within 30 min
        # (the re-submission loop QA reported on 2026-06-19).
        ctx.logger.info(
            "[TEMP-DBG] obs.invoice.submit",
            identity=ctx.identity,
            tool="extract_and_submit_base64",
            filename=filename,
            run_id=ctx.run_id,
            attempt_sig=attempt_sig or "",
            site="submit_first",
        )
        # Submit-first: backend creates the invoice immediately with
        # blank defaults (invoiceNumber='N/A', totalAmount=0,
        # customerName='N/A'); the OCR enrichment runs in the background
        # and lands as a later webhook. No synchronous OCR blocking here.
        try:
            response = await self._invoices.extract_and_submit_base64(
                access_token=token,
                filename=filename,
                content_base64=content,
                mime_type=mime,
            )
        except Exception as exc:  # noqa: BLE001
            ctx.logger.warning(
                "invoice_submit_first.failed",
                filename=filename, error=str(exc)[:300],
                error_type=type(exc).__name__,
            )
            # Per Ishan: NEVER block on OCR failures. The only resend
            # case is "no file bytes" (handled above). Anything else is
            # a transient backend hiccup — ask the SME to retry shortly.
            await self._send(
                ctx, state, "onboarding.invoice.failed",
                {
                    "reason": (
                        "We had a brief issue submitting your invoice — "
                        "please try once more in a minute. Your file is "
                        "fine; we just need another moment."
                    ),
                },
            )
            return self._step(
                "invoice_collect_await", ctx,
                access_token=token, refresh_token=refresh, token_expires_at=expires,
                **attempt_fields,
            )

        # [TEMP-DBG]
        self._dbg(
            ctx, "invoice.submit_first.response",
            response_keys=sorted(response.keys()) if isinstance(response, dict) else None,
            invoice_id=(
                response.get("invoice_id") or response.get("id")
                if isinstance(response, dict) else None
            ),
            backend_status=(
                response.get("status") or response.get("internalStatus")
                if isinstance(response, dict) else None
            ),
        )

        invoice_id = (
            response.get("invoice_id") or response.get("id")
            if isinstance(response, dict)
            else None
        )

        # Send the "submitted ✅" ack. OCR runs server-side; backend
        # webhook later carries the enriched details.
        await self._send(ctx, state, "onboarding.invoice.submitted")

        # Local ledger — record the submission so QA/status queries
        # can answer "what did I just send?" without waiting on the
        # enrichment webhook.
        ledger_entry = {
            "invoice_id": invoice_id,
            "filename": filename,
            "submitted_at": ctx.clock.now().isoformat(),
            "status": "SUBMITTED",
        }
        # UAT 2026-06-19 QA #2: record the sig so later resumes silently
        # drop this attachment payload (re-submission guard).
        new_sigs = list(state.invoice_submitted_sigs or [])
        if attempt_sig:
            new_sigs.append(attempt_sig)
        return self._step(
            "invoice_collect_await", ctx,
            invoices_submitted=[*state.invoices_submitted, ledger_entry],
            invoice_submitted_sigs=new_sigs[-200:],
            access_token=token, refresh_token=refresh, token_expires_at=expires,
            **attempt_fields,
        )

    async def _invoice_extract_for_confirm(
        self,
        state: OnboardingState,
        ctx: WorkflowContext,
        attachment: dict[str, Any],
        token: str | None,
        refresh: str | None,
        expires: int | None,
        *,
        attempt_sig: str | None = None,
    ) -> dict[str, Any]:
        """UAT 2026-06-16 #3 — single PDF flow.

        Call extract-only, store the draft + bytes on state, render the
        confirm card with Approve/Edit/Reject interactive buttons.
        """
        content = attachment.get("content_base64") or ""
        filename = attachment.get("filename") or "invoice.pdf"
        mime = attachment.get("mime_type")

        # UAT 2026-06-17 dedupe: record the attempt timestamp + fingerprint
        # on every return path so a poll-driven re-resume that re-delivers
        # the same attachment hits the dedupe check above.
        attempt_fields: dict[str, Any] = {}
        if attempt_sig:
            attempt_fields["last_invoice_attempt_sig"] = attempt_sig
            attempt_fields["last_invoice_attempt_at"] = (
                ctx.clock.now().isoformat()
            )

        if not content or not token:
            await self._send(
                ctx, state, "onboarding.invoice.failed",
                {"reason": "We didn't receive the file bytes — please resend."},
            )
            return self._step(
                "invoice_collect_await", ctx,
                access_token=token, refresh_token=refresh, token_expires_at=expires,
                **attempt_fields,
            )

        # UAT 2026-06-16 (PM): immediate ack BEFORE the cluster call.
        # The extract step can take 60-90s; without this the SME was
        # watching a silent chat between their upload and the confirm
        # card, then sometimes wandered off and missed the buttons.
        try:
            await self._send(ctx, state, "onboarding.invoice.processing")
        except Exception as exc:  # noqa: BLE001 — never block extract on the ack
            ctx.logger.warning(
                "invoice_processing_ack.failed", error=str(exc)[:200],
            )

        # [TEMP-DBG] To find exact bug - Temp Logs
        self._dbg(
            ctx, "invoice.extract.request",
            filename=filename,
            mime=mime,
            content_b64_len=len(content),
            access_token_len=len(token),
        )
        try:
            draft = await self._invoices.extract_base64(
                access_token=token,
                filename=filename,
                content_base64=content,
                mime_type=mime,
            )
        except Exception as exc:  # noqa: BLE001
            ctx.logger.warning(
                "invoice_extract.failed", filename=filename, error=str(exc)[:200],
            )
            # [TEMP-DBG]
            self._dbg(
                ctx, "invoice.extract.exception",
                filename=filename,
                error=str(exc)[:300],
                error_type=type(exc).__name__,
            )
            reason = (
                "Our invoice processor is taking longer than usual — please "
                "try again in a minute. Your file looks fine; we just need "
                "another moment to get it through."
                if _looks_like_transport_timeout(exc)
                else "We couldn't read the file — please resend as a clear "
                     "PDF or photo."
            )
            await self._send(
                ctx, state, "onboarding.invoice.failed", {"reason": reason},
            )
            return self._step(
                "invoice_collect_await", ctx,
                access_token=token, refresh_token=refresh, token_expires_at=expires,
                **attempt_fields,
            )

        # [TEMP-DBG] To find exact bug - Temp Logs: log the draft fingerprint
        # so we can see EXACTLY which OCR fields landed (or didn't) without
        # logging the full PII payload.
        self._dbg(
            ctx, "invoice.extract.draft",
            filename=filename,
            draft_keys=sorted(draft.keys()) if isinstance(draft, dict) else None,
            has_supplier=bool(isinstance(draft, dict) and draft.get("supplier_name")),
            has_customer=bool(isinstance(draft, dict) and draft.get("customer_name")),
            has_total=bool(isinstance(draft, dict) and draft.get("total_amount")),
            has_invoice_number=bool(isinstance(draft, dict) and draft.get("invoice_number")),
            has_due_date=bool(isinstance(draft, dict) and draft.get("due_date")),
            document_type=(draft.get("document_type") if isinstance(draft, dict) else None),
        )
        # UAT 2026-06-18 (Ishan diagnosis): the backend's invoice create
        # already accepts partial / empty data (defaults invoiceNumber to
        # 'N/A', totalAmount to '0', customerName to 'N/A') — ops fills
        # the rest manually. Render the confirm card REGARDLESS so the
        # SME can always Approve; em-dashes show where OCR fell short.
        # Only "no file bytes" still hard-fails (handled at line 5538).
        # Empty drafts are still logged so we can see where OCR struggled.
        if _draft_is_empty(draft):
            ctx.logger.warning(
                "invoice_extract.empty_draft",
                filename=filename,
                draft_keys=sorted(draft.keys()),
                note="rendering confirm card anyway per product call",
            )

        # Render the confirm card with 3 buttons. Title strings on the
        # buttons drive the SME-side text reply via the WhatsApp
        # interactive callback — match what _classify_confirm_action
        # parses.
        await self._send_invoice_confirm_card(ctx, state, draft)

        return self._step(
            "invoice_collect_await", ctx,
            pending_invoice_draft=draft,
            pending_invoice_filename=filename,
            pending_invoice_content_b64=content,
            pending_invoice_mime=mime,
            pending_invoice_edit_field=None,
            access_token=token, refresh_token=refresh, token_expires_at=expires,
            **attempt_fields,
        )

    async def _send_invoice_confirm_card(
        self,
        ctx: WorkflowContext,
        state: OnboardingState,
        draft: dict[str, Any],
    ) -> None:
        """Render the confirm body + Approve/Edit/Reject buttons.
        Falls back to plain text when the interactive send path declines
        — the SME can still type "Approve" / "Edit" / "Reject"."""
        variables = _confirm_card_variables(draft)
        sent = False
        send_buttons = getattr(self._msg, "send_reply_buttons", None)
        if send_buttons is not None and ctx.channel is Channel.WHATSAPP:
            try:
                sent = await send_buttons(
                    channel=_channel(ctx),
                    identity=ctx.identity,
                    template_key="onboarding.invoice.confirm",
                    buttons=[
                        ("invoice_approve", "Approve"),
                        ("invoice_edit",    "Edit"),
                        ("invoice_reject",  "Reject"),
                    ],
                    variables=variables,
                    locale=state.locale,
                )
            except Exception as exc:  # noqa: BLE001 — fall back to text
                ctx.logger.warning(
                    "invoice_confirm.buttons_failed", error=str(exc)[:200],
                )
        if not sent:
            await self._send(ctx, state, "onboarding.invoice.confirm", variables)

    async def _handle_invoice_confirm(
        self,
        state: OnboardingState,
        ctx: WorkflowContext,
        reply: Any,
        action: str,
    ) -> dict[str, Any]:
        """UAT 2026-06-16 #3 — process Approve/Edit/Reject.

        * approve → call submit_base64 with the (possibly edited)
          fields, append to ledger, send "submitted" message, clear
          draft from state.
        * edit    → if SME also named the field+value in one message
                    apply directly; otherwise ask which field, park.
        * reject  → discard draft, confirm.
        """
        draft = state.pending_invoice_draft or {}

        if action == "approve":
            return await self._invoice_submit_confirmed(state, ctx, draft)
        if action == "reject":
            await self._send(
                ctx, state, "onboarding.invoice.rejected",
                {
                    "ref": str(draft.get("invoice_number") or draft.get("invoice_id") or "—"),
                },
            )
            return self._step(
                "invoice_collect_await", ctx,
                pending_invoice_draft=None,
                pending_invoice_filename=None,
                pending_invoice_content_b64=None,
                pending_invoice_mime=None,
                pending_invoice_edit_field=None,
            )
        # action == "edit"
        # Pattern 1: "Edit amount: 32000" — single-line update.
        text = reply_text(reply).strip()
        field, value = _parse_edit_field_value(text)
        if field is not None and value is not None:
            updated = _apply_invoice_edit(draft, field, value)
            await self._send_invoice_confirm_card(ctx, state, updated)
            return self._step(
                "invoice_collect_await", ctx,
                pending_invoice_draft=updated,
                pending_invoice_edit_field=None,
            )
        # Pattern 2: SME just tapped "Edit" with no field — ask which.
        await self._send(
            ctx, state, "onboarding.invoice.edit.prompt",
            {"summary": _confirm_card_variables(draft)["summary"]},
        )
        return self._step(
            "invoice_collect_await", ctx,
            pending_invoice_edit_field="awaiting_field",
        )

    async def _invoice_submit_confirmed(
        self,
        state: OnboardingState,
        ctx: WorkflowContext,
        draft: dict[str, Any],
    ) -> dict[str, Any]:
        """Submit the confirmed (possibly edited) draft via
        ``submit_base64`` and clear the pending state."""
        token, refresh, expires = await self._live_token(state, ctx)
        if not token or not state.pending_invoice_content_b64:
            await self._send(
                ctx, state, "onboarding.invoice.failed",
                {"reason": "Lost the file bytes mid-confirm. Please resend the invoice."},
            )
            return self._step(
                "invoice_collect_await", ctx,
                pending_invoice_draft=None,
                pending_invoice_filename=None,
                pending_invoice_content_b64=None,
                pending_invoice_mime=None,
                pending_invoice_edit_field=None,
                access_token=token, refresh_token=refresh, token_expires_at=expires,
            )

        def _opt_str(key: str) -> str | None:
            v = draft.get(key)
            return None if v in (None, "") else str(v)

        # UAT 2026-06-16 (PM): ack the moment the SME taps Approve.
        # The cluster's submit step can take several seconds; without
        # this the chat is silent between the Approve tap and the
        # receipt template. Best-effort — never block the submit on
        # the ack send.
        try:
            await self._send(ctx, state, "onboarding.invoice.submitting")
        except Exception as exc:  # noqa: BLE001
            ctx.logger.warning(
                "invoice_submit_ack.failed", error=str(exc)[:200],
            )

        # [TEMP-DBG] obs.invoice.submit
        ctx.logger.info(
            "[TEMP-DBG] obs.invoice.submit",
            identity=ctx.identity,
            tool="submit_base64",
            filename=state.pending_invoice_filename or "—",
            run_id=ctx.run_id,
            attempt_sig=state.last_invoice_attempt_sig or "",
            site="submit_confirmed",
        )
        try:
            record = await self._invoices.submit_base64(
                access_token=token,
                filename=(
                    state.pending_invoice_filename
                    or draft.get("filename")
                    or "invoice.pdf"
                ),
                content_base64=state.pending_invoice_content_b64,
                mime_type=state.pending_invoice_mime,
                invoice_number=_opt_str("invoice_number"),
                invoice_date=_opt_str("invoice_date"),
                due_date=_opt_str("due_date"),
                total_amount=_opt_str("total_amount"),
                supplier_name=_opt_str("supplier_name"),
                customer_name=_opt_str("customer_name"),
            )
        except Exception as exc:  # noqa: BLE001
            ctx.logger.warning(
                "invoice_submit.failed", error=str(exc)[:200],
            )
            await self._send(
                ctx, state, "onboarding.invoice.failed",
                {
                    "reason": "We couldn't submit the invoice just now — please "
                              "try again in a moment or call +974 3017 3888.",
                },
            )
            return self._step(
                "invoice_collect_await", ctx,
                pending_invoice_draft=None,
                pending_invoice_filename=None,
                pending_invoice_content_b64=None,
                pending_invoice_mime=None,
                pending_invoice_edit_field=None,
                access_token=token, refresh_token=refresh, token_expires_at=expires,
            )

        now_iso = ctx.clock.now().isoformat()
        accepted = [_normalize_invoice_record(record, now_iso)]
        await self._send(
            ctx, state, "onboarding.invoice.received",
            {
                "summary": _format_accepted_invoices(accepted),
                "count":   1,
                "noun":    "invoice",
                "failed":  0,
            },
        )
        # UAT 2026-06-19 QA #2: record the attempt sig in the run-
        # lifetime submitted-sigs tracker so any later resume
        # (status_poll / phase1b webhook) that re-delivers the same
        # attachment payload silently drops at the routing fork.
        new_sigs = list(state.invoice_submitted_sigs or [])
        if state.last_invoice_attempt_sig:
            new_sigs.append(state.last_invoice_attempt_sig)
        return self._step(
            "invoice_collect_await", ctx,
            invoices_submitted=[*state.invoices_submitted, *accepted],
            invoice_submitted_sigs=new_sigs[-200:],
            pending_invoice_draft=None,
            pending_invoice_filename=None,
            pending_invoice_content_b64=None,
            pending_invoice_mime=None,
            pending_invoice_edit_field=None,
            access_token=token, refresh_token=refresh, token_expires_at=expires,
        )

    async def _invoice_bulk_submit_first(
        self,
        state: OnboardingState,
        ctx: WorkflowContext,
        attachments: list[dict[str, Any]],
        token: str | None,
        refresh: str | None,
        expires: int | None,
        *,
        attempt_sig: str | None = None,
    ) -> dict[str, Any]:
        """UAT 2026-06-18 (Ishan QA): bulk ZIP submit-first.

        Mirrors the single-file ``_invoice_submit_first`` contract for
        each ZIP member. The old ``_invoice_bulk_preview`` flow used
        ``extract_base64`` and DROPPED any member whose OCR failed
        (3 skip sites: nested-ZIP, no-bytes, extract-exception) — the
        SME lost invoices silently. This method never drops:

        * For each member, call ``extract_and_submit_base64`` (the
          submit path). Backend creates the invoice instantly with
          blank defaults (invoiceNumber='N/A', totalAmount='0',
          customerName='N/A') and enriches via OCR in the background.
        * Backend-side exception → the member counts as a failure but
          the SME is told about it in the consolidated receipt; we do
          NOT silently discard.
        * One consolidated "📦 Got your N invoices — all submitted ✅"
          receipt at the end. No CSV preview / APPROVE ALL handshake.

        Truly degenerate cases (no file bytes at all, no auth token,
        nested ZIP we couldn't unpack) are surfaced in the receipt so
        the SME isn't lied to.
        """
        attempt_fields: dict[str, Any] = {}
        if attempt_sig:
            attempt_fields["last_invoice_attempt_sig"] = attempt_sig
            attempt_fields["last_invoice_attempt_at"] = (
                ctx.clock.now().isoformat()
            )

        members, saw_zip = _expand_zip_attachments(attachments)
        # [TEMP-DBG] To find exact bug - Temp Logs
        self._dbg(
            ctx, "invoice.bulk.received",
            attachment_count=len(attachments),
            member_count=len(members),
            saw_zip=saw_zip,
        )
        if not members:
            await self._send(
                ctx, state, "onboarding.invoice.failed",
                {"reason": "We didn't see any invoice files in that batch. Please resend."},
            )
            return self._step(
                "invoice_collect_await", ctx,
                access_token=token, refresh_token=refresh, token_expires_at=expires,
                **attempt_fields,
            )

        # Immediate ack so the SME never sits in silence during the
        # submit fan-out. Best-effort — never blocks the submit chain.
        try:
            await self._send(ctx, state, "onboarding.invoice.processing")
        except Exception as exc:  # noqa: BLE001
            ctx.logger.warning(
                "invoice_bulk.processing_ack_failed", error=str(exc)[:200],
            )

        submitted: list[dict[str, Any]] = []
        failed: list[dict[str, Any]] = []
        new_ledger: list[dict[str, Any]] = []

        for idx, att in enumerate(members, 1):
            if _is_zip_attachment(att):
                # Nested ZIP we couldn't unpack — surface it; never silently drop.
                failed.append({
                    "filename": att.get("filename") or f"nested-zip-{idx}",
                    "reason": "nested ZIP could not be unpacked",
                })
                continue
            content = att.get("content_base64") or ""
            filename = att.get("filename") or f"invoice_{idx}.pdf"
            mime = att.get("mime_type")
            if not content:
                failed.append({
                    "filename": filename,
                    "reason": "empty file bytes",
                })
                continue
            if not token:
                failed.append({
                    "filename": filename,
                    "reason": "lost auth session — please try again",
                })
                continue
            # [TEMP-DBG]
            self._dbg(
                ctx, "invoice.bulk.member.submit",
                idx=idx, filename=filename, content_b64_len=len(content),
            )
            ctx.logger.info(
                "[TEMP-DBG] obs.invoice.submit",
                identity=ctx.identity,
                tool="extract_and_submit_base64",
                filename=filename,
                run_id=ctx.run_id,
                attempt_sig=attempt_sig or "",
                site="bulk_submit_first_member",
            )
            try:
                response = await self._invoices.extract_and_submit_base64(
                    access_token=token,
                    filename=filename,
                    content_base64=content,
                    mime_type=mime,
                )
            except Exception as exc:  # noqa: BLE001
                ctx.logger.warning(
                    "invoice_bulk.submit_failed",
                    idx=idx, filename=filename, error=str(exc)[:300],
                    error_type=type(exc).__name__,
                )
                failed.append({
                    "filename": filename,
                    "reason": "submit failed — please retry",
                })
                continue

            invoice_id = (
                response.get("invoice_id") or response.get("id")
                if isinstance(response, dict)
                else None
            )
            submitted.append({
                "invoice_id": invoice_id,
                "filename": filename,
            })
            new_ledger.append({
                "invoice_id": invoice_id,
                "filename": filename,
                "submitted_at": ctx.clock.now().isoformat(),
                "status": "SUBMITTED",
            })
            # [TEMP-DBG]
            self._dbg(
                ctx, "invoice.bulk.member.ok",
                idx=idx, filename=filename, invoice_id=invoice_id,
            )

        # Receipt. Send a consolidated receipt regardless of mixed
        # outcomes — we never go silent. Failures are listed so the SME
        # can retry only what didn't make it.
        if submitted:
            failure_block = (
                "\n\n⚠️ A few didn't go through this time — please resend "
                "just those:\n" + "\n".join(
                    f"• {f['filename']}" for f in failed[:10]
                )
                if failed
                else ""
            )
            noun = "invoice" if len(submitted) == 1 else "invoices"
            await self._send(
                ctx, state, "onboarding.invoice.bulk.submitted",
                {
                    "count": str(len(submitted)),
                    "noun": noun,
                    "details": "",
                    "failure_block": failure_block,
                },
            )
        else:
            # Everything failed — be honest and ask for a retry.
            await self._send(
                ctx, state, "onboarding.invoice.failed",
                {
                    "reason": (
                        "We had a brief issue submitting your invoices — "
                        "please try once more in a minute."
                    ),
                },
            )

        return self._step(
            "invoice_collect_await", ctx,
            invoices_submitted=[*state.invoices_submitted, *new_ledger],
            access_token=token, refresh_token=refresh, token_expires_at=expires,
            **attempt_fields,
        )

    async def _invoice_bulk_preview(
        self,
        state: OnboardingState,
        ctx: WorkflowContext,
        attachments: list[dict[str, Any]],
        token: str | None,
        refresh: str | None,
        expires: int | None,
        *,
        attempt_sig: str | None = None,
    ) -> dict[str, Any]:
        """UAT 2026-06-16 #4 — bulk ZIP / multi-file preview path.

        Local-unzip → per-member ``extract_base64`` → CSV-style table
        rendered inline (WhatsApp doesn't yet expose a document-send
        tool agent-side) → park awaiting APPROVE ALL / EDIT / REMOVE.
        Rows are flagged when extraction is unreliable OR when batch
        total > available credit limit (priority-order note follows).
        """
        # UAT 2026-06-17 dedupe: record the attempt sig + timestamp on
        # state via the first ``self._step`` return path so the routing
        # fork recognises a redelivered ZIP within the dedupe window. The
        # remaining return paths inherit the marker via state merge.
        attempt_fields: dict[str, Any] = {}
        if attempt_sig:
            attempt_fields["last_invoice_attempt_sig"] = attempt_sig
            attempt_fields["last_invoice_attempt_at"] = (
                ctx.clock.now().isoformat()
            )

        members, saw_zip = _expand_zip_attachments(attachments)
        if not members:
            await self._send(
                ctx, state, "onboarding.invoice.failed",
                {"reason": "We didn't see any invoice files in that batch. Please resend."},
            )
            return self._step(
                "invoice_collect_await", ctx,
                access_token=token, refresh_token=refresh, token_expires_at=expires,
                **attempt_fields,
            )

        batch: list[dict[str, Any]] = []
        failed = 0
        transport_timeout_failures = 0
        total_qar = 0
        currency = "QAR"
        row_num = 1
        for att in members:
            if _is_zip_attachment(att):
                # A ZIP that local-unzip couldn't expand — skip it from the
                # preview so the SME isn't told to "approve" a black box.
                failed += 1
                continue
            content = att.get("content_base64") or ""
            filename = att.get("filename") or f"invoice_{row_num}.pdf"
            mime = att.get("mime_type")
            if not content or not token:
                failed += 1
                continue
            try:
                draft = await self._invoices.extract_base64(
                    access_token=token,
                    filename=filename,
                    content_base64=content,
                    mime_type=mime,
                )
            except Exception as exc:  # noqa: BLE001
                ctx.logger.warning(
                    "invoice_extract.failed",
                    filename=filename, error=str(exc)[:200],
                )
                if _looks_like_transport_timeout(exc):
                    transport_timeout_failures += 1
                failed += 1
                continue

            amount = draft.get("total_amount")
            try:
                amount_int = int(float(amount)) if amount is not None else 0
            except (TypeError, ValueError):
                amount_int = 0
            total_qar += amount_int
            if isinstance(draft.get("currency"), str):
                currency = draft["currency"]
            flag = _flag_for_row(draft)
            batch.append({
                "row":          row_num,
                "draft":        draft,
                "filename":     filename,
                "content_b64":  content,
                "mime":         mime,
                "flag":         flag,
            })
            row_num += 1

        if not batch:
            reason = (
                "Our invoice processor is taking longer than usual — please "
                "try again in a minute."
                if transport_timeout_failures and transport_timeout_failures == failed
                else "We couldn't read any of those invoices — please resend "
                     "as clear PDFs or photos."
            )
            await self._send(
                ctx, state, "onboarding.invoice.failed", {"reason": reason},
            )
            return self._step(
                "invoice_collect_await", ctx,
                access_token=token, refresh_token=refresh, token_expires_at=expires,
            )

        # Check batch total vs available limit; flag the priority note
        # when total exceeds limit.
        available_limit = await self._fetch_available_limit(state, ctx, token)
        priority_note = ""
        if available_limit is not None and total_qar > available_limit:
            priority_note = (
                f"\n\n⚠️ Total {_fmt_qar(total_qar, currency)} exceeds your "
                f"available limit ({_fmt_qar(available_limit, currency)}). "
                "Invoices will be processed in priority order until the "
                "limit is reached."
            )

        table = _render_invoice_batch_table(batch, currency, total_qar)
        await self._send(
            ctx, state, "onboarding.invoice.batch.preview",
            {
                "table":         table + priority_note,
                "count":         str(len(batch)),
                "total":         _fmt_qar(total_qar, currency),
                "saw_zip":       "yes" if saw_zip else "no",
                "failed":        str(failed),
            },
        )

        return self._step(
            "invoice_collect_await", ctx,
            pending_invoice_batch=batch,
            pending_invoice_batch_total_qar=total_qar,
            pending_invoice_batch_currency=currency,
            access_token=token, refresh_token=refresh, token_expires_at=expires,
        )

    async def _fetch_available_limit(
        self,
        state: OnboardingState,
        ctx: WorkflowContext,
        token: str | None,
    ) -> int | None:
        """Read the SME's currently-available credit limit from /me's
        creditLine, returning an int or None if not reachable."""
        if not token:
            return None
        try:
            info = await self._identity.me(access_token=token)
        except Exception as exc:  # noqa: BLE001
            ctx.logger.warning(
                "invoice_batch.limit_lookup_failed", error=str(exc)[:200]
            )
            return None
        line = _extract_credit_line(info)
        avail = line.get("availableLimit") or line.get("available_limit")
        try:
            return int(float(avail)) if avail is not None else None
        except (TypeError, ValueError):
            return None

    async def _handle_invoice_batch_action(
        self,
        state: OnboardingState,
        ctx: WorkflowContext,
        reply: Any,
        action: str,
    ) -> dict[str, Any]:
        """Apply APPROVE ALL / EDIT <row>: <change> / REMOVE <row> to a
        pending batch. Returns the next workflow step."""
        if action == "approve_all":
            return await self._invoice_batch_submit_all(state, ctx)
        if action == "reject_all":
            await self._send(ctx, state, "onboarding.invoice.batch.cleared", {})
            return self._step(
                "invoice_collect_await", ctx,
                pending_invoice_batch=[],
                pending_invoice_batch_total_qar=None,
                pending_invoice_batch_currency=None,
            )

        text = reply_text(reply).strip()
        if action == "remove":
            row = _parse_row_number(text)
            if row is None:
                await self._send(
                    ctx, state, "onboarding.invoice.batch.help",
                    {"hint": "I couldn't read the row number — try `remove 3`."},
                )
                return self._step("invoice_collect_await", ctx)
            new_batch = _remove_batch_row(state.pending_invoice_batch, row)
            new_total, currency = _sum_batch_total(new_batch)
            if not new_batch:
                await self._send(
                    ctx, state, "onboarding.invoice.batch.cleared", {},
                )
                return self._step(
                    "invoice_collect_await", ctx,
                    pending_invoice_batch=[],
                    pending_invoice_batch_total_qar=None,
                    pending_invoice_batch_currency=None,
                )
            table = _render_invoice_batch_table(new_batch, currency, new_total)
            await self._send(
                ctx, state, "onboarding.invoice.batch.preview",
                {
                    "table":  table,
                    "count":  str(len(new_batch)),
                    "total":  _fmt_qar(new_total, currency),
                    "saw_zip": "yes",
                    "failed":  "0",
                },
            )
            return self._step(
                "invoice_collect_await", ctx,
                pending_invoice_batch=new_batch,
                pending_invoice_batch_total_qar=new_total,
                pending_invoice_batch_currency=currency,
            )

        # action == "edit"
        row, field, value = _parse_batch_edit(text)
        if row is None or field is None or value is None:
            await self._send(
                ctx, state, "onboarding.invoice.batch.help",
                {
                    "hint": (
                        "I need row + field + new value. Try "
                        "`edit 2: amount 32000` or `edit 3: due 2026-07-28`."
                    ),
                },
            )
            return self._step("invoice_collect_await", ctx)
        new_batch = _apply_batch_edit(state.pending_invoice_batch, row, field, value)
        new_total, currency = _sum_batch_total(new_batch)
        table = _render_invoice_batch_table(new_batch, currency, new_total)
        await self._send(
            ctx, state, "onboarding.invoice.batch.preview",
            {
                "table":  table,
                "count":  str(len(new_batch)),
                "total":  _fmt_qar(new_total, currency),
                "saw_zip": "yes",
                "failed":  "0",
            },
        )
        return self._step(
            "invoice_collect_await", ctx,
            pending_invoice_batch=new_batch,
            pending_invoice_batch_total_qar=new_total,
            pending_invoice_batch_currency=currency,
        )

    async def _send_invoice_csv(
        self,
        ctx: WorkflowContext,
        state: OnboardingState,
        csv_text: str,
        *,
        count: int,
        total: int,
        currency: str,
    ) -> bool:
        """Send the batch as a CSV document + an APPROVE ALL / edit prompt.

        Returns True iff the CSV document was delivered (False → caller falls
        back to the inline table preview)."""
        content_b64 = base64.b64encode(csv_text.encode("utf-8")).decode("ascii")
        caption = (
            f"📄 Your {count} invoice(s) — review sheet. Open it, edit the value "
            "after any label, and send the file back to correct them — or tap "
            "Approve all / Reject all below."
        )
        try:
            sent = await self._msg.send_document(
                channel=_channel(ctx),
                identity=ctx.identity,
                filename="invoices_review.txt",
                content_base64=content_b64,
                caption=caption,
                mime_type="text/plain",
                locale=state.locale,
            )
        except Exception as exc:  # noqa: BLE001 — degrade to inline table
            ctx.logger.warning(
                "invoice_csv.send_document_failed", error=str(exc)[:200],
            )
            return False
        if not sent:
            return False
        # Document delivered → follow up with the APPROVE ALL prompt + how-to.
        prompt_vars = {"count": str(count), "total": _fmt_qar(total, currency)}
        sent_btn = False
        send_buttons = getattr(self._msg, "send_reply_buttons", None)
        if send_buttons is not None:
            try:
                sent_btn = await send_buttons(
                    channel=_channel(ctx),
                    identity=ctx.identity,
                    template_key="onboarding.invoice.batch.csv_review",
                    buttons=[("batch_approve_all", "Approve all"), ("batch_reject_all", "Reject all")],
                    variables=prompt_vars,
                    locale=state.locale,
                )
            except Exception as exc:  # noqa: BLE001
                ctx.logger.warning(
                    "invoice_csv.prompt_buttons_failed", error=str(exc)[:200],
                )
                sent_btn = False
        if not sent_btn:
            try:
                await self._send(
                    ctx, state, "onboarding.invoice.batch.csv_review", prompt_vars,
                )
            except Exception as exc:  # noqa: BLE001
                ctx.logger.warning(
                    "invoice_csv.prompt_failed", error=str(exc)[:200],
                )
        return True

    async def _handle_edited_csv(
        self,
        state: OnboardingState,
        ctx: WorkflowContext,
        csv_text: str,
    ) -> dict[str, Any]:
        """Reconcile an SME-edited CSV against the pending batch (match by row
        number, falling back to position) and submit the corrected rows."""
        parsed = _parse_invoice_batch_csv(csv_text)
        existing = list(state.pending_invoice_batch or [])
        by_row: dict[int, dict[str, Any]] = {}
        for entry in existing:
            try:
                by_row[int(entry.get("row"))] = entry
            except (TypeError, ValueError):
                continue
        new_batch: list[dict[str, Any]] = []
        new_row = 1
        for idx, parsed_row in enumerate(parsed, 1):
            entry = by_row.get(parsed_row.get("row"))
            if entry is None and idx - 1 < len(existing):
                entry = existing[idx - 1]
            if entry is None or not (entry.get("content_b64") or ""):
                # Can't submit a row with no underlying file bytes.
                continue
            draft = dict(entry.get("draft") or {})
            for field in _CSV_COLUMNS:
                value = parsed_row.get(field)
                if value in (None, ""):
                    continue
                if field == "total_amount":
                    try:
                        draft[field] = int(float(str(value).replace(",", "")))
                    except (TypeError, ValueError):
                        draft[field] = value
                else:
                    draft[field] = value
            updated = dict(entry)
            updated["draft"] = draft
            updated["row"] = new_row
            updated["flag"] = _flag_for_row(draft)
            new_batch.append(updated)
            new_row += 1
        if not new_batch:
            # Unreadable / unmatched CSV — never drop their invoices silently.
            await self._send(
                ctx, state, "onboarding.invoice.batch.help",
                {
                    "hint": (
                        "I couldn't read that CSV. Edit the cells (keep the "
                        "header row) and resend, or reply APPROVE ALL to submit "
                        "as-is."
                    ),
                },
            )
            return self._step("invoice_collect_await", ctx)
        return await self._invoice_batch_submit_all(state, ctx, batch=new_batch)

    async def _invoice_batch_submit_all(
        self,
        state: OnboardingState,
        ctx: WorkflowContext,
        batch: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        """Submit every row in the pending batch via ``submit_base64``
        and append the resulting records to the ledger. Clears the
        batch on success or partial success. ``batch`` overrides
        ``state.pending_invoice_batch`` (used by the edited-CSV path)."""
        target_batch = batch if batch is not None else (state.pending_invoice_batch or [])
        token, refresh, expires = await self._live_token(state, ctx)
        accepted: list[dict[str, Any]] = []
        failed = 0
        now_iso = ctx.clock.now().isoformat()
        if not token:
            await self._send(
                ctx, state, "onboarding.invoice.failed",
                {"reason": "Lost authentication — please resend the batch."},
            )
            return self._step(
                "invoice_collect_await", ctx,
                pending_invoice_batch=[],
                pending_invoice_batch_total_qar=None,
                pending_invoice_batch_currency=None,
                access_token=token, refresh_token=refresh, token_expires_at=expires,
            )

        def _opt_field(d: dict[str, Any], key: str) -> str | None:
            v = d.get(key)
            return None if v in (None, "") else str(v)

        # UAT 2026-06-19 QA #5: PARALLEL submit. Sequential submits made
        # 8 invoices take >10 minutes. Cap at 5 in-flight (matches the
        # cluster's documented concurrency budget). Each row goes through
        # submit_base64 independently so a single transient failure
        # doesn't drag the rest down.
        import asyncio as _asyncio
        # OCR is single-worker (serializes); issuing N extracts in
        # parallel makes the later ones time out waiting in its queue
        # (UAT 2026-06-20 bulk failure). Extract ONE at a time so each
        # call gets a fresh per-call timeout. Slower but reliable.
        sem = _asyncio.Semaphore(1)

        async def _submit_one(row: dict[str, Any]) -> dict[str, Any] | None:
            draft = row.get("draft") or {}
            content = row.get("content_b64") or ""
            ctx.logger.info(
                "[TEMP-DBG] obs.invoice.submit",
                identity=ctx.identity,
                tool="submit_base64",
                filename=row.get("filename") or "invoice.pdf",
                run_id=ctx.run_id,
                attempt_sig=state.last_invoice_attempt_sig or "",
                site="bulk_approve_all_row",
            )
            async with sem:
                try:
                    return await self._invoices.submit_base64(
                        access_token=token,
                        filename=row.get("filename") or "invoice.pdf",
                        content_base64=content,
                        mime_type=row.get("mime"),
                        invoice_number=_opt_field(draft, "invoice_number"),
                        invoice_date=_opt_field(draft, "invoice_date"),
                        due_date=_opt_field(draft, "due_date"),
                        total_amount=_opt_field(draft, "total_amount"),
                        supplier_name=_opt_field(draft, "supplier_name"),
                        customer_name=_opt_field(draft, "customer_name"),
                    )
                except Exception as exc:  # noqa: BLE001
                    ctx.logger.warning(
                        "invoice_batch_submit.failed",
                        row=row.get("row"), error=str(exc)[:200],
                    )
                    return None

        # [TEMP-DBG]
        self._dbg(
            ctx, "invoice.bulk_submit_all.start",
            row_count=len(target_batch),
        )
        results = await _asyncio.gather(
            *(_submit_one(row) for row in target_batch)
        )
        for record in results:
            if record is None:
                failed += 1
            else:
                accepted.append(_normalize_invoice_record(record, now_iso))

        # UAT 2026-06-19 QA #1: ONE consolidated bulk receipt. Replaces
        # the per-row "submitted ✅" spam — SME sees just the summary.
        failure_block = (
            f"\n\n⚠️ {failed} didn't submit cleanly — please resend those."
            if failed else ""
        )
        noun = "invoice" if len(accepted) == 1 else "invoices"
        await self._send(
            ctx, state, "onboarding.invoice.bulk.submitted",
            {
                "count":         str(len(accepted)),
                "noun":          noun,
                "details":       _render_submitted_details(target_batch),
                "failure_block": failure_block,
            },
        )
        # UAT 2026-06-19 QA #2: record the sig now that the SME has
        # explicitly approved the batch — any later resume that carries
        # the same attachment payload silently drops.
        new_sigs = list(state.invoice_submitted_sigs or [])
        if state.last_invoice_attempt_sig:
            new_sigs.append(state.last_invoice_attempt_sig)
        return self._step(
            "invoice_collect_await", ctx,
            invoices_submitted=[*state.invoices_submitted, *accepted],
            invoice_submitted_sigs=new_sigs[-200:],
            pending_invoice_batch=[],
            pending_invoice_batch_total_qar=None,
            pending_invoice_batch_currency=None,
            access_token=token, refresh_token=refresh, token_expires_at=expires,
        )

    async def _handle_invoice_qa(
        self,
        state: OnboardingState,
        ctx: WorkflowContext,
        intent: str,
    ) -> dict[str, Any]:
        """Render a structured self-service answer at invoice_collect_await.

        Three intents (UAT 2026-06-16 #9):
        * ``limit``           — approved credit limit + currently available.
        * ``disbursed_total`` — sum of disbursements so far on this run.
        * ``due``             — outstanding balance + count of pending EMIs.

        All answers prefer real backend reads (``/me`` for limit + credit
        line, the in-state ledgers for disbursement/repayment totals).
        When a read fails we fall back to whatever's on state — that lets
        us still answer accurately for invoices the SME submitted in
        THIS run even if /me hiccups.
        """
        token, refresh, expires = await self._live_token(state, ctx)

        if intent == _QAIntent.LIMIT:
            currency, limit, available = "QAR", None, None
            if token:
                try:
                    info = await self._identity.me(access_token=token)
                except Exception as exc:  # noqa: BLE001
                    ctx.logger.warning(
                        "invoice_qa.me_failed", error=str(exc)[:200]
                    )
                    info = {}
                line = _extract_credit_line(info)
                currency = line.get("currency") or currency
                limit = line.get("creditLimit") or line.get("credit_limit")
                available = (
                    line.get("availableLimit") or line.get("available_limit")
                )
            answer = _format_limit_answer(currency, limit, available)
        elif intent == _QAIntent.DISBURSED_TOTAL:
            total, currency = _sum_disbursements(state.disbursements_received)
            answer = _format_disbursed_answer(total, currency)
        else:  # DUE
            outstanding = state.repayment_outstanding_qar
            currency = "QAR"
            # Sum remaining EMIs from the latest repayment record on state
            # (backend is the source of truth — we just expose the most
            # recent snapshot we have on hand).
            emis_remaining = _latest_emis_remaining(state.repayments_recorded)
            answer = _format_due_answer(currency, outstanding, emis_remaining)

        await self._send(
            ctx, state, "onboarding.help.contextual",
            {"answer": answer, "next_step": ""},
        )
        return self._step(
            "invoice_collect_await", ctx,
            access_token=token, refresh_token=refresh, token_expires_at=expires,
        )

    async def _handle_phase1b_event(
        self,
        state: OnboardingState,
        ctx: WorkflowContext,
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        """Dispatch a Phase 1.b webhook (canonical entry from
        ``_invoice_collect_await``). Renders the SME-facing template +
        updates the ledger, then returns the agent to
        ``invoice_collect_await``.

        Other wait nodes (journey/lender) call
        :meth:`_apply_phase1b_event_to_state` instead so a misrouted
        webhook doesn't force them to leave their current step.
        """
        return await self._apply_phase1b_event_to_state(
            state, ctx, payload, target_step="invoice_collect_await",
        )

    async def _apply_phase1b_event_to_state(
        self,
        state: OnboardingState,
        ctx: WorkflowContext,
        payload: dict[str, Any],
        *,
        target_step: str,
    ) -> dict[str, Any]:
        """Render the SME-facing template + update the ledger from a
        Phase 1.b webhook payload, returning a ``_step`` result with
        ``target_step``. This lets non-canonical wait nodes process
        the event without forcing a node change.

        UAT 2026-06-16 (PM audit): backend SHOULD only fire Phase 1.b
        events after credit_line.activated when the SME is parked at
        invoice_collect_await. But race conditions (delayed
        credit_line.activated; SME mid-journey when a backend retry
        fires) could land the event at journey_wait_await or
        lender_wait_await — without this seam those nodes silently
        re-parked and the ledger update was lost.

        ``payload`` carries ``event`` (the backend event_type) plus all
        the webhook's fields verbatim — amount, ref, due_date, etc. The
        templates substitute the same field names so the message body
        reflects whatever the backend sent.
        """
        event = str(payload.get("event") or "")
        now_iso = ctx.clock.now().isoformat()

        # Best-effort field reader: backend uses camelCase, our older test
        # fixtures use snake_case — accept both.
        def _get(*keys: str) -> Any:
            for k in keys:
                v = payload.get(k)
                if v not in (None, ""):
                    return v
            return None

        def _fmt_money(value: Any, currency: str) -> str:
            try:
                return f"{currency} {int(float(value)):,}"
            except (TypeError, ValueError):
                return f"{currency} {value}" if value not in (None, "") else f"{currency} —"

        # Canonical payload fields per Madad PR #187 + UAT 2026-06-16 notes.
        # transaction.disbursed: invoiceNumber, disbursedAmount, utr.
        # repayment.received / .closed (single event, branch on `closed`):
        #   invoiceNumber, amount, totalRepaid, outstandingAmount, emisTotal,
        #   emisPaid, emisRemaining, paymasterName, lenderName, availableLimit,
        #   currency, dueDate, closed (bool).
        currency = _get("currency") or "QAR"
        invoice_ref = _get(
            "invoiceNumber", "invoice_number",
            "invoice_id", "invoiceId",
            "reference_number", "referenceNumber", "ref",
        )

        # State accumulators — append to whichever ledger the event maps to.
        disbursements = list(state.disbursements_received)
        repayments = list(state.repayments_recorded)
        outstanding = state.repayment_outstanding_qar

        if event == "transaction.disbursed":
            disbursed = _get(
                "disbursedAmount", "disbursed_amount",
                "amount", "amount_qar", "amountQar",
                "total_amount", "totalAmount",
            )
            utr = _get("utr", "UTR", "transaction_id", "transactionId")
            due_date = _get("dueDate", "due_date")
            # UAT 2026-06-19 QA #7: Meta-approved disbursement template
            # expects 6 vars (ref, amount, lender, utr, due_date,
            # available_limit) — the agent was only passing 4 so the
            # SME's message rendered "🏦 Lender: —" and "Available limit
            # remaining: QAR —". Backend payload IS carrying lenderName,
            # availableLimit, creditLimit per Madad PR #195/#196 (QA-
            # confirmed). Wire all of them.
            lender = _get("lenderName", "lender_name", "lender") or "—"
            available_limit = _get(
                "availableLimit", "available_limit",
                "credit_limit_available",
            )
            credit_limit = _get(
                "creditLimit", "credit_limit", "total_credit_limit",
            )
            disbursements.append({
                "amount": disbursed,
                "currency": currency,
                "invoice_ref": invoice_ref,
                "utr": utr,
                "due_date": due_date,
                "lender": lender,
                "available_limit": available_limit,
                "credit_limit": credit_limit,
                "received_at": now_iso,
                "payload": payload,
            })
            await self._send(
                ctx, state, "onboarding.disbursement.received",
                {
                    "amount":          _fmt_money(disbursed, currency),
                    "ref":             invoice_ref or "—",
                    "utr":             str(utr) if utr else "—",
                    "due_date":        str(due_date) if due_date else "—",
                    "lender":          lender,
                    "available_limit": (
                        _fmt_money(available_limit, currency)
                        if available_limit is not None
                        else "—"
                    ),
                    "credit_limit": (
                        _fmt_money(credit_limit, currency)
                        if credit_limit is not None
                        else "—"
                    ),
                },
            )
        elif event in {"repayment.received", "repayment.partially_paid", "repayment.closed"}:
            # Madad PR #187 unified flow: a single ``repayment.received``
            # event with ``closed`` (bool) tells us whether this payment
            # cleared every EMI. ``repayment.closed`` and
            # ``repayment.partially_paid`` are kept as event types for
            # back-compat — both branch through the same rendering logic
            # using either the explicit event suffix or the closed flag.
            amount_this = _get("amount", "amount_qar", "amountQar")
            total_repaid = _get("totalRepaid", "total_repaid")
            outstanding_amount = _get(
                "outstandingAmount", "outstanding_amount",
                "outstanding", "outstanding_qar", "outstandingQar",
                "remaining",
            )
            emis_total = _get("emisTotal", "emis_total")
            emis_paid = _get("emisPaid", "emis_paid")
            emis_remaining = _get("emisRemaining", "emis_remaining")
            paymaster = _get("paymasterName", "paymaster_name", "paymaster")
            lender = _get("lenderName", "lender_name", "lender")
            available_limit = _get(
                "availableLimit", "available_limit", "credit_limit_available",
            )
            due_date = _get("dueDate", "due_date")
            closed_flag = payload.get("closed")
            is_closed = (
                bool(closed_flag)
                if closed_flag is not None
                else event == "repayment.closed"
            )
            # Outstanding: when fully closed it's 0 regardless of what the
            # backend sent; otherwise the explicit number wins, falling
            # back to None when nothing was provided.
            if is_closed:
                outstanding = 0
            elif isinstance(outstanding_amount, (int, float)):
                outstanding = int(outstanding_amount)
            else:
                outstanding = None

            repayments.append({
                "amount": amount_this,
                "currency": currency,
                "invoice_ref": invoice_ref,
                "total_repaid": total_repaid,
                "outstanding": outstanding,
                "emis_total": emis_total,
                "emis_paid": emis_paid,
                "emis_remaining": emis_remaining,
                "paymaster_name": paymaster,
                "lender_name": lender,
                "available_limit": available_limit,
                "due_date": due_date,
                "kind": "closed" if is_closed else "received",
                "received_at": now_iso,
            })

            template_key = (
                "onboarding.repayment.closed" if is_closed
                else "onboarding.repayment.received"
            )
            variables: dict[str, Any] = {
                "amount":          _fmt_money(amount_this, currency),
                "ref":             invoice_ref or "—",
                "total_repaid":    _fmt_money(total_repaid, currency),
                "outstanding":     _fmt_money(outstanding_amount, currency),
                "emis_total":      str(emis_total) if emis_total is not None else "—",
                "emis_paid":       str(emis_paid) if emis_paid is not None else "—",
                "emis_remaining":  str(emis_remaining) if emis_remaining is not None else "—",
                "paymaster":       str(paymaster) if paymaster else "—",
                "lender":          str(lender) if lender else "—",
                "available_limit": _fmt_money(available_limit, currency),
                "due_date":        str(due_date) if due_date else "—",
            }
            await self._send(ctx, state, template_key, variables)
        elif event == "repayment.due_soon":
            due_date = _get("dueDate", "due_date")
            amount_due = _get(
                "amount", "amount_qar", "amountQar", "amountDue", "amount_due",
            )
            await self._send(
                ctx, state, "onboarding.repayment.due_soon",
                {
                    "amount":   _fmt_money(amount_due, currency),
                    "ref":      invoice_ref or "—",
                    "due_date": str(due_date) if due_date else "soon",
                },
            )
        elif event == "repayment.overdue":
            days_overdue = _get("daysOverdue", "days_overdue")
            amount_due = _get(
                "amount", "amount_qar", "amountQar",
                "outstandingAmount", "outstanding_amount",
            )
            await self._send(
                ctx, state, "onboarding.repayment.overdue",
                {
                    "amount":       _fmt_money(amount_due, currency),
                    "ref":          invoice_ref or "—",
                    "days_overdue": str(days_overdue) if days_overdue else "—",
                },
            )
        else:
            # Defensive — unknown phase1b event, stay parked silently.
            ctx.logger.warning("phase1b_event.unknown", event=event)

        # Thread ``last_status_source`` from the payload so the poller's
        # suppression-window logic still applies (a webhook just arrived
        # → skip the next 1-2 cadence ticks). Phase 1.b events from the
        # dispatcher always carry source="webhook".
        return self._step(
            target_step,
            ctx,
            disbursements_received=disbursements,
            repayments_recorded=repayments,
            repayment_outstanding_qar=outstanding,
            last_status_source=_extract_status_source(payload),
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
        # UAT 2026-06-16 (afternoon): waiver path. ``qualified.waived``
        # OR a post-payment status hint (ACCEPTED/OFFER_ACCEPTED/
        # ACTIVATED) landed at the docs loop — the SME shouldn't go
        # through the TESS payment chain because the fee is already
        # settled. Jump STRAIGHT to ``lender_status_poll`` instead of
        # ``business_details_fetch`` (which would create + send a
        # payment link the SME doesn't need).
        if state.paid or state.journey_status in {
            JourneyStatus.ACCEPTED,
            JourneyStatus.OFFER_ACCEPTED,
            JourneyStatus.ACTIVATED,
        }:
            return self._dbg_route(
                "_route_documents", state, "lender",
                reason="paid_or_post_payment_status",
            )
        # Refinement per Ishan (UAT 2026-06-09): when admin QUALIFIES
        # mid-docs-loop, jump STRAIGHT to the payment chain. The
        # ``documents_complete`` coffee message ("🎊 all documents
        # received") is misleading in that case — the checklist isn't
        # actually complete; admin overrode it. ``payment_ready`` is
        # set on the same forced-status branch in
        # _documents_upload_loop_await, so this route catches the
        # override and bypasses both ``documents_complete`` and the
        # short-circuited ``payment_wait_await`` stop.
        if state.payment_ready or state.journey_status == JourneyStatus.QUALIFIED:
            return self._dbg_route(
                "_route_documents", state, "payment",
                reason="payment_ready_or_qualified",
            )
        # Frustrated-user escape hatch: the SME replied NO to "any more
        # documents?" while some required docs were still undetected — proceed
        # to the next step (the payment-wait park) without the coffee.
        if state.docs_proceed:
            return self._dbg_route(
                "_route_documents", state, "proceed",
                reason="docs_proceed=true",
            )
        # TRUE completion: every required doc detected. Show the coffee /
        # "all documents received" message exactly ONCE (user 2026-06-12),
        # then re-park silently. A classifier hang that leaves a required slot
        # pending does NOT auto-complete here — instead the in-loop "any more
        # documents?" prompt fires and the SME replies NO to proceed.
        if not state.missing_documents:
            decision = "complete" if not state.documents_complete_sent else "await_again"
            return self._dbg_route(
                "_route_documents", state, decision,
                reason="no_missing_docs",
                documents_complete_sent=state.documents_complete_sent,
            )
        return self._dbg_route(
            "_route_documents", state, "await_again",
            reason="missing_docs_remaining",
            missing_count=len(state.missing_documents),
        )

    def _route_more_docs(self, state: OnboardingState) -> str:
        decision = (state.more_docs_decision or "").lower()
        if decision == "yes":
            return "yes"
        if decision == "no":
            return "no"
        return "await_again"

    def _route_prequalify_wait(self, state: OnboardingState) -> str:
        if state.prequalification_rejected:
            return "rejected"
        return "go" if state.prequalified else "wait"

    def _route_payment_wait(self, state: OnboardingState) -> str:
        # UAT 2026-06-16 waiver path: backend's ``qualified.waived``
        # webhook (or any post-payment status_update) sets paid=True
        # via the in-node handler. We then jump STRAIGHT to the lender
        # poll, bypassing business_details_fetch → payment_create →
        # payment_send_link (which would create a TESS link the SME
        # doesn't need on the waived path).
        if state.paid:
            return self._dbg_route(
                "_route_payment_wait", state, "lender",
                reason="paid=True_waiver_path",
            )
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
            return self._dbg_route(
                "_route_payment_wait", state, "go",
                reason="payment_ready=True",
            )
        if state.journey_status in {
            JourneyStatus.QUALIFIED,
            JourneyStatus.ACCEPTED,
            JourneyStatus.OFFER_ACCEPTED,
            JourneyStatus.ACTIVATED,
        }:
            return self._dbg_route(
                "_route_payment_wait", state, "go",
                reason="journey_status_post_qualify",
            )
        return self._dbg_route(
            "_route_payment_wait", state, "wait",
            reason="not_paid_not_ready",
        )

    def _route_payment(self, state: OnboardingState) -> str:
        decision = "paid" if state.paid else "unpaid"
        return self._dbg_route("_route_payment", state, decision)

    def _route_journey_status(self, state: OnboardingState) -> str:
        s = state.journey_status
        if s in (JourneyStatus.PRE_QUALIFIED, JourneyStatus.QUALIFIED):
            # UAT 2026-06-14: after offer handoff the run parks at
            # journey_wait_await; the status poller fires from there and a
            # transient PRE_QUALIFIED / QUALIFIED read from /me used to
            # re-route through business_details_fetch → payment_send_link,
            # showing the SME a SECOND "Pay QAR X →" button AFTER offers
            # were already in. If they've already paid, stay parked and
            # wait for the next real status update.
            return "wait" if state.paid else "payment"
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
        # At the documents step, hand the model the REAL doc state so it can
        # answer "what have I sent / what's still needed" accurately, in-chat,
        # without redirecting to a portal or guessing (user 2026-06-14). This
        # enriches only the LLM context; the user-facing nudge stays concise.
        llm_hint = hint
        last_step = state.history[-1].step if state.history else ""
        if last_step in {"documents_upload_loop_send", "documents_upload_loop_await"}:
            _missing = list(state.missing_documents or [])
            _received = [d for d in DEFAULT_WHATSAPP_REQUIRED_DOCS if d not in _missing]
            _recv_str = ", ".join(
                DOCUMENT_LABELS.get(d, d.replace("_", " ").title()) for d in _received
            ) or "none yet"
            _miss_str = ", ".join(
                DOCUMENT_LABELS.get(d, d.replace("_", " ").title()) for d in _missing
            ) or "none"
            llm_hint = (
                f"{hint}\nDocuments already received: {_recv_str}. "
                f"Documents still needed: {_miss_str}."
            )
        # Post-activation / under-review steps: hand Groq the REAL account
        # state so it answers status questions accurately and NEVER claims
        # "being reviewed" once the credit line is active (user 2026-06-20).
        if last_step in {"invoice_collect_send", "invoice_collect_await",
                         "journey_wait_await", "lender_wait_await"}:
            _facts: list[str] = []
            try:
                _st = state.journey_status.value if state.journey_status else None
            except Exception:  # noqa: BLE001
                _st = None
            if _st:
                _facts.append(f"current journey status = {_st}")
            if last_step in {"invoice_collect_send", "invoice_collect_await"}:
                _facts.append(
                    "the credit line is ACTIVE and the SME can submit invoices "
                    "for financing right here"
                )
            _ninv = len(state.invoices_submitted or [])
            if _ninv:
                _facts.append(
                    f"{_ninv} invoice(s) already submitted and now being "
                    "processed/reviewed by the Madad team for disbursement"
                )
            for _o in (state.offers or []):
                if not isinstance(_o, dict):
                    continue
                _ln = _lender_name(_o) or "a lender"
                _lim = _o.get("creditLimit") or _o.get("credit_limit") or _o.get("limit")
                _rt = _o.get("interestRate") or _o.get("interest_rate") or _o.get("rate")
                _tn = _o.get("tenureDays") or _o.get("tenure_days") or _o.get("tenure")
                _facts.append(
                    f"offer from {_ln}: credit limit QAR {_lim}, interest "
                    f"{_rt}%/mo, tenure {_tn} days"
                )
            if _facts:
                llm_hint = (
                    hint + "\nACTUAL ACCOUNT STATE (answer using THIS; do NOT "
                    "say the application is 'being reviewed' if the credit line "
                    "is active): " + "; ".join(_facts) + "."
                )
        answer = await _llm_answer(reply_text(reply), llm_hint)
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
        variables = variables or {}
        # 24h-window fix: status messages can land outside Meta's customer-care
        # window, where free text is silently dropped. Approved templates are
        # valid in AND out of the window, so for mapped status keys we always
        # send the template; ANY failure (build error, send rejected, no data)
        # falls through to the original free-text send below — so behaviour is
        # unchanged for every other message and there is no regression.
        tpl = _STATUS_TEMPLATES.get(template_key)
        if tpl and ctx.channel is Channel.WHATSAPP:
            try:
                comps = _status_components(template_key, variables, state)
                if comps is not None and await self._msg.send_template(
                    channel=_channel(ctx),
                    identity=ctx.identity,
                    template_name=tpl,
                    template_key=template_key,
                    language_code=locale or state.locale,
                    components=comps,
                    variables=variables,
                ):
                    return
            except Exception as exc:  # noqa: BLE001 — fall back to free text
                ctx.logger.warning(
                    "status_template.failed", key=template_key, error=str(exc)[:200]
                )
        await self._msg.send(
            channel=_channel(ctx),
            identity=ctx.identity,
            template_key=template_key,
            variables=variables,
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

    # -- [TEMP-DBG] To find exact bug - Temp Logs -----------------------------
    # The helpers below wrap every node + add structured tracing at the
    # key seams (entry, exit, exception, routing decisions). They're
    # intentionally chatty — remove this block when the pipeline is stable.

    def _dbg_wrap_node(
        self, node_name: str, fn: Any,
    ) -> Any:
        """[TEMP-DBG] Wrap a node so every call logs entry / exit / exception
        with the relevant state fingerprint. Lets ops see exactly which node
        produced a given log line + where execution diverged."""
        import time

        async def wrapper(state: OnboardingState, ctx: WorkflowContext) -> Any:
            ctx.logger.info(
                "[TEMP-DBG] node.enter",
                node=node_name,
                run_id=ctx.run_id,
                channel=str(ctx.channel),
                identity=ctx.identity,
                journey_status=str(state.journey_status)
                if state.journey_status is not None else None,
                paid=state.paid,
                payment_confirmed_sent=state.payment_confirmed_sent,
                docs_uploaded_count=state.docs_uploaded_count,
                missing_documents_count=len(state.missing_documents),
                has_access_token=bool(state.access_token),
                offers_count=len(state.offers or []),
                pending_invoice=bool(state.pending_invoice_draft),
                last_status_source=state.last_status_source,
            )
            t0 = time.monotonic()
            try:
                result = await fn(state, ctx)
            except Exception as exc:  # noqa: BLE001 — instrumentation, not handling
                elapsed_ms = int((time.monotonic() - t0) * 1000)
                # LangGraph's ``GraphInterrupt`` is normal control flow
                # (signals "wait for input") — NOT a real error. Skip the
                # node.exception log so the issue monitor isn't flooded
                # with false positives. Anything else is a real error.
                from langgraph.errors import GraphInterrupt
                if not isinstance(exc, GraphInterrupt):
                    ctx.logger.exception(
                        "[TEMP-DBG] node.exception",
                        node=node_name,
                        elapsed_ms=elapsed_ms,
                        error=str(exc)[:300],
                        error_type=type(exc).__name__,
                    )
                raise
            elapsed_ms = int((time.monotonic() - t0) * 1000)
            result_keys: list[str] = []
            if isinstance(result, dict):
                result_keys = sorted(
                    k for k in result.keys() if k != "history"
                )
            ctx.logger.info(
                "[TEMP-DBG] node.exit",
                node=node_name,
                elapsed_ms=elapsed_ms,
                result_keys=result_keys,
            )
            return result

        return wrapper

    def _dbg(
        self, ctx: WorkflowContext, event: str, **fields: Any,
    ) -> None:
        """[TEMP-DBG] Structured ad-hoc debug log keyed by event name."""
        ctx.logger.info(
            f"[TEMP-DBG] {event}",
            run_id=ctx.run_id,
            channel=str(ctx.channel),
            identity=ctx.identity,
            **fields,
        )

    def _dbg_route(
        self, route_name: str, state: OnboardingState, decision: str,
        **extra: Any,
    ) -> str:
        """[TEMP-DBG] Log a routing decision (no ctx — routes are sync
        and don't receive the WorkflowContext). Returns ``decision`` so
        the routing function can be wrapped inline."""
        from app.core.logging import get_logger
        get_logger("workflow.routing").info(
            "[TEMP-DBG] route.decision",
            route=route_name,
            decision=decision,
            journey_status=str(state.journey_status)
            if state.journey_status is not None else None,
            paid=state.paid,
            prequalified=state.prequalified,
            prequalification_rejected=getattr(
                state, "prequalification_rejected", False,
            ),
            payment_ready=state.payment_ready,
            documents_received=state.documents_received,
            offers_count=len(state.offers or []),
            **extra,
        )
        return decision


def _channel(ctx: WorkflowContext) -> Channel:
    assert ctx.channel is not None
    return ctx.channel


# ── 24h-safe status templates ────────────────────────────────────────────────
# Maps a CMS template_key (the free-text body used in-window) to its approved
# Meta WhatsApp template name (delivers IN and OUT of the 24h window). _send()
# prefers the template for these keys and falls back to the free text on any
# failure. See _status_components() for the per-template variable order.
_STATUS_TEMPLATES: dict[str, str] = {
    "onboarding.documents.checklist":   "onboarding_documents_checklist",
    "onboarding.payment.request":       "onboarding_payment_request",
    "onboarding.payment.confirmed":     "onboarding_payment_confirmed",
    # UAT 2026-06-18 (Ishan Bug 2): offer cards arrive days after the
    # SME left the chat — free-text was being silently dropped outside
    # the 24h Meta window. Both ``offers.preview`` (the cards list) and
    # ``offer.handoff`` (the CTA fallback) now use the same approved
    # ``onboarding_offers_available`` template so every offer notice
    # reliably delivers regardless of conversation age.
    "onboarding.offers.preview":        "onboarding_offers_available",
    "onboarding.offer.confirmed":       "onboarding_offer_confirmed",
    "onboarding.activated":             "onboarding_activated",
    "onboarding.disbursement.received": "onboarding_disbursement_received",
    "onboarding.repayment.received":    "onboarding_repayment_received",
    "onboarding.repayment.closed":      "onboarding_repayment_closed",
    "onboarding.repayment.due_soon":    "onboarding_repayment_due_soon",
    "onboarding.repayment.overdue":     "onboarding_repayment_overdue",
}


def _tpl_txt(v: Any) -> str:
    """A safe Meta body/button parameter: single line, never empty."""
    s = str(v if v is not None else "").replace("\n", " ").replace("\t", " ").strip()
    while "    " in s:
        s = s.replace("    ", " ")
    return s or "—"


def _strip_qar(v: Any) -> str:
    """Drop a leading 'QAR ' so a template that already prints 'QAR {{n}}' does
    not render 'QAR QAR 500,000'."""
    s = _tpl_txt(v)
    return s[4:].strip() if s.upper().startswith("QAR ") else s


def _offers_block(state: OnboardingState) -> str:
    """One line per offer for the offers_available Meta template's single
    {{1}} variable.

    UAT 2026-06-19 QA #6a fix: Meta WhatsApp body parameters strip plain
    ``\\n`` in some renders, so two offers showed up as 'QIB — ...
    Commercial Bank — ...' on a single line. Prepend each row with a
    leading newline + bullet glyph so the visual break survives whatever
    Meta does to the text — the glyph itself forces the eye to a new
    row even if newlines collapse, and the leading newline kicks the
    first row off the variable's same-line position when present.
    """
    rows: list[str] = []
    for o in (getattr(state, "offers", None) or []):
        if not isinstance(o, dict):
            continue
        lender = _lender_name(o) or "Bank"
        try:
            limit = f"QAR {int(o.get('creditLimit') or o.get('credit_limit') or o.get('limit') or 0):,}"
        except (TypeError, ValueError):
            limit = "QAR —"
        try:
            rate = f"{float(o.get('interestRate') or o.get('interest_rate') or o.get('rate') or 0):g}%/mo"
        except (TypeError, ValueError):
            rate = "—"
        try:
            tenure = f"{int(o.get('tenureDays') or o.get('tenure_days') or o.get('tenure') or 0)} days"
        except (TypeError, ValueError):
            tenure = "—"
        rows.append(f"🏦 {lender} — Limit: {limit} · Interest: {rate} · Tenure: {tenure}")
    # Double-newline separator: most Meta renderers preserve ``\n\n``
    # (paragraph break) even when collapsing single ``\n`` to a space.
    return "\n\n".join(rows) if rows else "Please log in to view your offer details."


def _status_components(
    key: str, variables: dict[str, Any], state: OnboardingState
) -> list[dict[str, Any]] | None:
    """Build Meta `components` for a status template from the per-event variables
    the workflow already passes to _send(). Returns ``None`` when a required
    value is missing → _send() falls back to free text (no broken send)."""
    v = variables or {}

    def body(*vals: Any) -> list[dict[str, Any]]:
        return [{"type": "body",
                 "parameters": [{"type": "text", "text": _tpl_txt(x)} for x in vals]}]

    if key == "onboarding.documents.checklist":
        return []  # static body, no variables
    if key == "onboarding.payment.confirmed":
        return body(v.get("provider_ref") or v.get("ref") or "—")
    if key == "onboarding.offer.confirmed":
        return body(v.get("lender"))
    if key == "onboarding.activated":
        return body(v.get("lender"), _strip_qar(v.get("limit")), v.get("rate"), v.get("tenure"))
    if key == "onboarding.disbursement.received":
        return body(
            v.get("ref"), _strip_qar(v.get("amount")), v.get("lender") or "—",
            v.get("utr"), v.get("due_date"),
            _strip_qar(v.get("available_limit")) if v.get("available_limit") else "—",
        )
    if key == "onboarding.repayment.received":
        return body(
            v.get("ref"), _strip_qar(v.get("amount")), _strip_qar(v.get("outstanding")),
            v.get("emis_paid"), v.get("emis_remaining"),
        )
    if key == "onboarding.repayment.closed":
        return body(
            v.get("ref"),
            _strip_qar(v.get("available_limit")) if v.get("available_limit") else "—",
        )
    if key == "onboarding.repayment.due_soon":
        return body(v.get("ref"), _strip_qar(v.get("amount")), v.get("due_date"))
    if key == "onboarding.repayment.overdue":
        return body(v.get("ref"), _strip_qar(v.get("amount")), v.get("due_date") or "—")
    if key == "onboarding.offers.preview":
        # Both map to the same approved ``onboarding_offers_available``
        # Meta template with a single {{1}} variable for the offer block.
        return body(_offers_block(state))
    if key == "onboarding.payment.request":
        pid = getattr(state, "payment_id", None)
        if not pid:
            return None  # no payment id → can't form the Pay button → free-text fallback
        return [
            {"type": "body", "parameters": [
                {"type": "text", "text": _tpl_txt(v.get("score"))},
                {"type": "text", "text": _strip_qar(v.get("amount"))},
            ]},
            {"type": "button", "sub_type": "url", "index": 0,
             "parameters": [{"type": "text", "text": _tpl_txt(pid)}]},
        ]
    return None


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
