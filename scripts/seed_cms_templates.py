"""Seed the CMS with demo content for every onboarding template key.

The workflow renders every conversational message through the CMS
(``CommunicationMessenger`` calls ``communication.send`` which calls
``cms.get_template``). Without seeded content, the rendered text is the
empty string — the demo shows blank messages.

This script writes one short English body per ``TEMPLATE_KEYS`` entry
into the CMS service (via the workflow service's own dependency-injected
CMS — the same one the messenger reads). It's idempotent: running it
twice overwrites with the same content.

Usage (inside the running workflow container)::

    docker compose -f docker/docker-compose.yml --env-file .env exec workflow \
        python -m scripts.seed_cms_templates

The script writes to whatever PERSISTENCE backend is configured (Postgres
in staging, in-memory in dev). Bring up the stack first.
"""

from __future__ import annotations

import asyncio
from typing import Any

from app.services.cms.deps import get_cms_service
from app.services.cms.enums import ConfigKind
from app.services.cms.models import ChecklistItem
from app.services.workflow import TEMPLATE_KEYS
from app.services.workflow.onboarding import DEFAULT_WHATSAPP_REQUIRED_DOCS, DOCUMENT_LABELS
from app.shared.i18n import Locale

# -- Nudge timing constants --------------------------------------------------
# The Madad flow doc (PDF) names three nudge cadences in Day units; the nudge
# scheduler stores offsets in seconds.
DAY = 24 * 60 * 60
DAY_1 = 1 * DAY
DAY_2 = 2 * DAY
DAY_3 = 3 * DAY
DAY_5 = 5 * DAY
DAY_7 = 7 * DAY
DAY_14 = 14 * DAY

# Demo template bodies — short, English, no Arabic for now (M-3 deferred).
# Operators can override any of these in the CMS admin endpoint after seeding.
_TEMPLATE_BODIES = {
    "onboarding.campaign.intro": (
        "Hello! 👋\n\n"
        "We are Madad (madadfintech.com) — a business finance company in Qatar. "
        "We provide financing support to unlock cash stuck with your clients in invoices.\n\n"
        "Are you interested in taking financing for your business?\n"
        "Reply YES or NO"
    ),
    "onboarding.campaign.awaiting_yes_no": (
        "Are you interested in taking financing for your business?\n"
        "Please reply YES or NO. For any query call +974 3017 3888."
    ),
    "onboarding.help.what_is_madad": (
        "Hello! 👋\n\n"
        "We are Madad (madadfintech.com) — a regulated business finance company "
        "in Qatar. We help businesses unlock working capital from invoices owed "
        "by enterprise or government clients.\n\n"
        "✅ Fast financing — funds within 5 working days of approval\n"
        "✅ Multiple bank offers — you choose\n"
        "✅ Sharia-compliant · Regulated by Qatar Central Bank\n\n"
        "You can verify us at madadfintech.com or call +974 3017 3888."
    ),
    "onboarding.help.security": (
        "Absolutely — your data is completely safe with us. 🔒\n\n"
        "We are a regulated entity under Qatar Central Bank. The consent simply "
        "means you agree that Madad may access your business information to "
        "assess eligibility and use the document for financing purposes. That's all.\n\n"
        "You can verify us at madadfintech.com or call +974 3017 3888."
    ),
    "onboarding.help.contextual": (
        "{{ answer }}\n\n"
        "{{ next_step }}\n\n"
        "For any query call +974 3017 3888."
    ),
    "onboarding.declined": (
        "No problem at all! If you ever need working capital support in the future, "
        "we're here. Reach us at madadfintech.com or call +974 3017 3888. Have a great day! 👋"
    ),
    "onboarding.domain_blocked": (
        "It looks like {{ domain }} is already registered with another team. "
        "Please contact your administrator or use a personal email."
    ),
    "onboarding.collect_details.request": (
        "Great to hear you're interested! 🎉\n\n"
        "To create your account please share:\n"
        "1) Your first + last name\n"
        "2) Your business's legal entity name\n"
        "3) Your CR (Commercial Registration) number\n"
        "4) Is your business based in Qatar?\n"
        "5) Your role at the business (FOUNDER / DIRECTOR / SHAREHOLDER / ...)"
    ),
    # Business-email step — asked right after YES / account creation, before
    # the consent/CR step. Capturing it makes the lead portal-loginable.
    "onboarding.business_email.ask": (
        "What's your business email? We'll use it for your account and to keep "
        "you updated on your application. 📧"
    ),
    "onboarding.business_email.conflict": (
        "Looks like a business is already registered with that email.\n\n"
        "Please reply with a different business email, or contact our support "
        "team at contactus@madadfintech.com and we'll help you out. 📧"
    ),
    "onboarding.consent.request": (
        "Great to know! 🎉 We have financed many businesses like yours in Qatar.\n\n"
        "To start the journey we need to first verify that your business is in Qatar "
        "and eligible for financing.\n\n"
        "We need your Commercial Registration (CR) to verify this.\n\n"
        "Before you share, please note:\n"
        "✅ We are a regulated entity under Qatar Central Bank\n"
        "🔗 Privacy Policy: https://www.madadfintech.com/en/privacy-policy\n"
        "🔒 Terms & Conditions: https://www.madadfintech.com/en/terms-and-conditions\n"
        "📄 Data and Credit Bureau Consent: "
        "https://portal.madadfintech.com/financialsConsent\n\n"
        "By sharing your CR you agree to the above. Please go ahead and share "
        "your CR document as a PDF or photo in this chat.\n\n"
        "Any questions? Reply here or call us on +974 3017 3888."
    ),
    "onboarding.eligibility.intake.request": (
        "Quick business questionnaire — please share: "
        "1) Are you Qatar-based? "
        "2) Business age in years "
        "3) CR validity (VALID / EXPIRED) "
        "4) Company type (LLC / SOLE / PARTNERSHIP) "
        "5) Sector "
        "6) Annual turnover (QAR) "
        "7) Number of employees"
    ),
    "onboarding.not_eligible": (
        "Based on our review, we can't proceed at this time. Please contact "
        "our team for more information."
    ),
    # UAT 2026-06-18 (Ishan WhatsApp #62): the CR upload used to fire two
    # back-to-back messages — ``cr.received`` ("📄 Got your CR — processing")
    # then this ``financials.request`` opened with "Awesome, thanks for
    # sharing!" + "To further assess your eligibility we need…". The
    # "thanks/eligibility" preamble was redundant with the cr.received
    # ack and a recursive reference to a step that no longer runs (the
    # eligibility questionnaire was removed in 62d7560). Trimmed to be
    # PURELY about asking for financials — the cr.received ack handles
    # the immediate feedback.
    "onboarding.financials.request": (
        # {{ cr_affirmation }} is the "registered in Qatar — all good" line, sent
        # ONLY when the CR step's upload classified as a real CR (else empty, so
        # a random/non-CR upload doesn't get a false Qatar-registration claim).
        "{{ cr_affirmation }}"
        "Please share your last Audited Financial Statement to complete "
        "your application.\n\n"
        "For any query call us on +974 3017 3888."
    ),
    "onboarding.buyers.request": (
        "Please share your main buyer's details (name, country, contact)."
    ),
    "onboarding.shareholders.request": (
        "Please share your shareholders' details (name, percentage)."
    ),
    "onboarding.account.created": (
        "Perfect! 🙌\n\n"
        "You will receive your pre-qualification result within 24 hours.\n\n"
        "Meanwhile, your account has been created on Madad with reference number "
        "#{{ ref }}. You can login at portal.madadfintech.com and track your status anytime."
    ),
    "onboarding.documents.checklist": (
        "🎉 Congratulations! Your business is pre-qualified for financing.\n\n"
        "You can have cash in your account within 5 working days of completing "
        "your application!\n\n"
        "We now need the following documents to complete your application:\n\n"
        "📁 Business Documents\n"
        "1. National Address Certificate\n"
        "2. Article of Association\n"
        "3. Establishment Card\n\n"
        "🏦 Financial Documents\n"
        "✅ Audited Report 2025 — already received\n"
        "4. Audited Report 2023\n"
        "5. Audited Report 2022\n"
        "6. Qatar Credit Bureau Report\n"
        "7. Payable Ageing Schedule\n"
        "8. Receivable Ageing Schedule\n"
        "9. Interim Financial Statement\n"
        "10. Bank Statement (last 6 months)\n\n"
        "👤 Shareholder Documents (per shareholder from CR)\n"
        "11. QID   12. Passport\n\n"
        "ℹ️ Optional: Shareholder Proof of Address — send if you have it, "
        "but not required to proceed.\n\n"
        "📤 Share the documents here or login at portal.madadfintech.com to complete "
        "your application.\n\n"
        "Please share your documents to move forward!"
    ),
    "onboarding.documents.missing": (
        "✅ Got it — {{ received }} of {{ total }} documents received! 🙌\n\n"
        "⏳ Still needed:\n"
        "{{ documents }}\n\n"
        "No rush — send them one at a time or all together."
    ),
    # Acknowledgement when the SME sends everything in one ZIP — header + the
    # per-document "Received & Validated" checklist built by the workflow.
    "onboarding.documents.zip_received": (
        "📦 ZIP received! Extracting your documents...\n\n"
        "{{ results }}"
    ),
    # Acknowledgement when one (or a few) loose files are sent — just the
    # per-document "Received & Validated" lines.
    "onboarding.documents.single_received": (
        "{{ results }}"
    ),
    "onboarding.documents.complete": (
        "🎊 Great — all documents received!\n\n"
        "⏳ Please wait while our team processes them — we’ll share your "
        "assessment report within 24 hours. If all looks good, we’ll forward "
        "your application to our banking partners in Qatar. 🏦\n\n"
        "Thank you for being with us. ☕\n\n"
        "For any query call +974 3017 3888 or visit madadfintech.com"
    ),
    "onboarding.upload.required": (
        "Whenever you're ready, please share {{ document }} as a PDF or photo here "
        "and I'll take it from there. 🙂\n\n"
        "Have a question? Just ask — happy to help. For any query call +974 3017 3888."
    ),
    # Per user (UAT 2026-06-10): after the coffee message we explicitly ask
    # the SME whether they have any more documents to send (classifier
    # failures + the "I forgot one" case). Reply YES / NO; existing
    # synonym set covers Ok / Sure / Nope / etc. WhatsApp interactive
    # reply-button send is on the cluster's backlog; until that ships,
    # this is plain-text + the synonym-aware matcher.
    "onboarding.documents.more_docs_prompt": (
        "📋 Thanks — here's where your application stands. Still pending:\n\n"
        "{{ documents }}\n\n"
        "Would you like to upload more documents?"
    ),
    # End-of-upload "settle" message (UAT 2026-06-13): the checklist + the
    # any-more prompt in ONE message, fired ONCE by the docs_more_prompt nudge
    # after the SME stops uploading — never mid-batch. ``{{ results }}`` is the
    # current checklist body supplied by the workflow when it arms the nudge.
    "onboarding.documents.settle_prompt": (
        "{{ results }}\n\n"
        "📄 Do you have any more documents to upload?\n\n"
        "Reply YES if you'd like to send more, or NO if you're done — "
        "we'll proceed with the next step."
    ),
    # Immediate ack the instant a valid CR attachment arrives — guarantees the
    # user always sees a response even if the downstream upload + financials
    # prompt fails (QA Bug #1 + Ishan handover §9 / 2026-06-09).
    # Neutral wording (prod 2026-07-02): we ack the upload BEFORE the classifier
    # decides, so we must NOT claim "Got your CR" yet — a non-CR (e.g. a random
    # screenshot) would then get contradicted by the reupload prompt. On a real
    # CR the flow proceeds to the financials request; on a non-CR it asks for a
    # proper re-upload (onboarding.cr.reupload).
    "onboarding.cr.received": (
        "📄 Got it — checking your document now…"
    ),
    # Sent when the classifier RAN and confidently decided the upload is NOT a
    # Commercial Registration. Nudges a proper re-upload. Capped in code
    # (_CR_MAX_REUPLOAD_NUDGES) so a genuine CR the classifier can't read still
    # gets through afterwards — the SME is never trapped.
    "onboarding.cr.reupload": (
        "Hmm — I couldn't verify that document as a Commercial Registration (CR). 📄\n\n"
        "Please re-upload a clear copy of your CR as a PDF or a well-lit photo "
        "showing the full document. If you're sure this is your CR, just send it "
        "again and we'll continue.\n\n"
        "For any query call us on +974 3017 3888."
    ),
    # UAT 2026-06-16 (PM): same pattern for the audited financial
    # statement — the upload + account-create round-trip used to be
    # silent on the chat between the SME's send and the
    # ``onboarding.account.created`` message.
    "onboarding.financials.received": (
        "📊 Got your audited financial statement — processing it now…\n\n"
        "Thank you for being with us. 🙏"
    ),
    # Immediate ack on any document upload in the post-prequal docs loop.
    # Rewritten 2026-06-09 (UAT feedback): the previous copy assumed the
    # SME sent a ZIP and was wordy / unprofessional. Now generic, short,
    # and channel-agnostic so it fits both single-file and ZIP uploads.
    "onboarding.documents.processing": (
        "📄 Got it — please wait while we process your document(s). "
        "It may take up to 10 minutes.\n\n"
        "Thank you for being with us. ☕"
    ),
    # Final fallback sent at the end of the docs loop when neither the
    # classifier nor the local-unzip pipeline could land a single file the
    # backend accepted. Keeps the SME informed instead of dropping silent.
    "onboarding.documents.upload_failed": (
        "I received your file(s) but couldn't process them right now. "
        "Please resend any failed documents as separate PDF/photo uploads, "
        "or call +974 3017 3888 if it keeps happening."
    ),
    "onboarding.status.pending": (
        "Hi! Your application is currently under review with Madad. 👍\n\n"
        "I'll notify you as soon as the next update is available. You can also "
        "track your status at portal.madadfintech.com. For queries call +974 3017 3888."
    ),
    # Re-state nudge (fires when the SME asks a question / re-pings while the
    # payment is still open). NO hardcoded amount — the exact, dynamic fee is
    # already on the "Pay QAR {{ amount }} →" button/link from
    # onboarding.payment.request(.button), so this only points them back to it.
    # (Prod 2026-07-02: a hardcoded "QAR 6,000" here contradicted the real
    # product amount shown on the link.)
    "onboarding.payment.awaiting": (
        "Your application is ready to move forward. Please complete the secure "
        "onboarding and assessment fee payment using the link shared above.\n\n"
        "Once payment is received, your application will be forwarded immediately."
    ),
    # PDF Step 6 — fires from _payment_await on paid=True. Bank list comes from
    # BusinessDetails.banksToSend (admin sets this when forwarding to lenders).
    "onboarding.payment.confirmed": (
        "🎉 Thank you — payment received!\n\n"
        "Your application has been forwarded to our banking partners in Qatar: "
        "{{ banks }}.\n\n"
        "We will update you as soon as financing offers are received — typically "
        "within 3–5 business days. 📲\n\n"
        "Track your status at portal.madadfintech.com (Ref: {{ ref }})"
    ),
    # UAT 2026-06-17 RCA fix: sent when the admin marks the user as
    # ``qualified.waived`` (qualified + onboarding fee waived). Replaces
    # the silent-advance behaviour added on 2026-06-16 — backend does not
    # actually send a customer-facing waiver message, so the SME was
    # left in silence after the coffee message.
    "onboarding.qualified.waived": (
        "🎊 Great news — you're qualified!\n\n"
        "Your onboarding fee has been waived, so there's nothing to pay. "
        "Your application is now being reviewed by our lender partners — "
        "we'll send you their offers as soon as they're ready. 📲\n\n"
        "For any query, call +974 3017 3888."
    ),
    "onboarding.not_qualified": (
        "Unfortunately your application wasn't accepted by our lender "
        "partners this time. Please reach out to our team."
    ),
    # UAT 2026-06-17 gap fix: SME parked at prequalify_wait_await when the
    # admin marked them as not pre-qualified. Send a clear next-steps
    # message instead of leaving them in silence.
    "onboarding.not_pre_qualified": (
        "Thank you for your interest in Madad. After reviewing your "
        "business profile, we are unable to pre-qualify your application "
        "for financing at this time.\n\n"
        "If your situation changes (new financials, new clients, business "
        "growth), please reach out to us — we're happy to take another look.\n\n"
        "For any query, call +974 3017 3888."
    ),
    # UAT 2026-06-17: terminal for SMEs whose CR shows the business is
    # registered outside Qatar. Madad's financing is Qatar-only.
    "onboarding.not_qatar": (
        "Thank you for reaching out! Unfortunately, Madad's financing is "
        "available only to businesses registered in Qatar.\n\n"
        "If your business is currently registered elsewhere, please feel "
        "free to come back when you have a Qatar entity.\n\n"
        "For any query, call +974 3017 3888."
    ),
    # Body shown ABOVE the "Pay QAR … →" CTA button (no raw link — the button
    # carries it). Used for the interactive WhatsApp send.
    "onboarding.payment.request.button": (
        "Hello! 👋\n\n"
        "Your application has been reviewed by our team. Here is your result:\n\n"
        "{{ score_line }}"
        "Your application is ready to be forwarded.\n"
        "To submit your application to the banks, a one-time onboarding and assessment "
        "fee of QAR {{ amount }} is required.\n\n"
        "Once payment is received, your application will be forwarded immediately."
    ),
    # Plain-text fallback (when interactive buttons are unavailable) — keeps the
    # tappable link inline.
    "onboarding.payment.request": (
        "Hello! 👋\n\n"
        "Your application has been reviewed by our team. Here is your result:\n\n"
        "{{ score_line }}"
        "Your application is ready to be forwarded.\n"
        "To submit your application to the banks, a one-time onboarding and assessment "
        "fee of QAR {{ amount }} is required.\n\n"
        "Pay QAR {{ amount }} →\n"
        "{{ payment_link }}\n\n"
        "Once payment is received, your application will be forwarded immediately."
    ),
    # Per user (UAT 2026-06-10): the offers preview + the handoff message
    # used to fire as TWO separate WhatsApp bubbles and the offer cards
    # rendered empty on top. Both are now folded into the single
    # ``onboarding.offer.handoff.button`` CTA-URL message below, so this
    # template stays as a back-compat fallback only — kept for tests + any
    # email channel still rendering it standalone.
    "onboarding.offers.preview": (
        "🎉 Exciting news — your financing offers are ready!\n\n"
        "{{ offer_cards }}\n\n"
        "💬 Feel free to ask me anything about these offers right here!"
    ),
    # Step 8.5 — the SME selected/accepted an offer in the Madad portal
    # (backend offer.selected webhook). One-time ✅ confirmation; the run then
    # parks for the credit-line activation message below.
    "onboarding.offer.confirmed": (
        "✅ Confirmed — {{ lender }} selected!\n\n"
        "Madad and {{ lender }} will now coordinate the formalities.\n"
        "You'll hear from us within 2 business days. 🤝\n\n"
        "Once your credit line is active, you can submit invoices right here on "
        "WhatsApp — I'll guide you through that step too."
    ),
    # PDF Step 9 — credit line activated, surfaces the accepted offer details
    # (bank, limit, rate, tenure) inline so the SME has the key numbers in
    # hand without opening the platform.
    "onboarding.activated": (
        "🎊 Your financing line is ACTIVE!\n\n"
        "🏦 {{ lender }} · 💰 {{ limit }} · 📈 {{ rate }} · ⏱ {{ tenure }}\n\n"
        "You can now submit invoices for financing right here — send a single "
        "PDF or multiple invoices at once. 📄\n\n"
        "Track at portal.madadfintech.com (Ref: {{ ref }})"
    ),
    # Returning-SME re-greeting (UAT 2026-06-28). The agent fills two slots
    # from the route detected by ``madad_mcp_check_registration``:
    #   {{ scenario }} — the route-specific message ("Your credit line is
    #                     already active — send any invoice…", or similar);
    #                     the 7 options live in ``_registered_route_send``.
    #   {{ ref_suffix }} — " (Ref: #N)" when a reference exists, else "".
    # WhatsApp uses the approved template ``onboarding_welcome_back``
    # ({{1}}, {{2}} positional params). Email + free-text fallback render
    # this body directly, so the leading "Welcome back!" and trailing
    # "For any queries…" live HERE so they appear on every channel.
    "onboarding.welcome_back": (
        "👋 Welcome back!\n\n"
        "{{ scenario }}\n\n"
        "For any queries, reply here or call 72773652.{{ ref_suffix }}"
    ),
    # ----------------------------------------------------------------
    # Phase 1.b — invoice financing (Steps 10–13).
    # ----------------------------------------------------------------
    # UAT 2026-06-16 (PM): immediate ack the moment the SME's invoice
    # PDF lands — fires BEFORE the cluster's extract round-trip (which
    # can take 60–90s) so the SME isn't watching a silent chat.
    "onboarding.invoice.processing": (
        "📄 Got your invoice — reading it now…\n\n"
        "This may take a little while — we'll confirm here as soon as it's done. ⏳\n\n"
        "Thank you for being with us. 🙏"
    ),
    # UAT 2026-06-18 (Ishan Bug 1) — SUBMIT-FIRST ack. Replaces the old
    # confirm-card → Approve → submit flow with one immediate confirmation.
    # Backend creates the invoice instantly (blank defaults, ops fills the
    # rest) and enriches via OCR in the background; the SME just needs to
    # know it landed.
    "onboarding.invoice.submitted": (
        "📄 Got your invoice — submitted ✅\n\n"
        "We'll confirm the details shortly. Our team will review and "
        "you'll get an update here once it's disbursed. 💸\n\n"
        "Send another invoice anytime — single file or a ZIP both work."
    ),
    # UAT 2026-06-18 (Ishan QA): bulk submit-first receipt — one message
    # for the whole batch. ``failure_block`` is empty on full success and
    # carries a short bullet list of the (rare) per-member failures otherwise.
    "onboarding.invoice.bulk.submitted": (
        "📦 Got your {{ count }} {{ noun }} — all submitted ✅\n\n"
        "{{ details }}"
        "We'll confirm the details shortly. Our team will review each "
        "and you'll get an update here once they're disbursed. 💸{{ failure_block }}\n\n"
        "Send more invoices anytime — single file or a ZIP both work."
    ),
    # UAT 2026-06-19 QA #1: ONE consolidated ack fires up front when a
    # ZIP / multi-file batch lands. Replaces the per-file "reading it
    # now…" spam that hit the SME with 10+ messages per upload.
    "onboarding.invoice.bulk.processing": (
        "📦 Received {{ count }} invoices — processing.\n\n"
        "We'll send you the review summary shortly — this may take a little while. ⏳\n\n"
        "Thank you for being with us. 🙏"
    ),
    # Fires AFTER the SME taps Approve on the confirm card — the
    # cluster's submit step can take a few seconds and the SME used
    # to see a silent chat between Approve and the receipt. UAT
    # 2026-06-16 (PM).
    "onboarding.invoice.submitting": (
        "📤 Submitting your invoice for financing…\n\n"
        "This usually takes a moment — we’ll confirm here as soon as "
        "it’s in. ⏳"
    ),
    # Receipt after each invoice submission. ``summary`` is the
    # per-invoice ✅ block built by ``_format_accepted_invoices``;
    # ``count`` / ``noun`` / ``failed`` reflect the batch.
    "onboarding.invoice.received": (
        "{{ summary }}\n\n"
        "Got it — {{ count }} {{ noun }} submitted for financing. "
        "Our team will review and you'll get an update here once it's disbursed. 💸\n\n"
        "Send another invoice anytime — single file or a ZIP both work."
    ),
    # Submission failed (unreadable file / no token / backend error).
    "onboarding.invoice.failed": (
        "⚠️ {{ reason }}\n\n"
        "Need help? Reply here or call us on +974 3017 3888."
    ),
    # On-demand "what's the status of my invoices?" reply.
    "onboarding.invoice.status": (
        "📋 Your invoice history ({{ count }}):\n\n"
        "{{ summary }}\n\n"
        "Send a new invoice anytime as a PDF or photo."
    ),
    # UAT 2026-06-16 (#3): single-PDF confirm card body shown alongside
    # the 3 interactive buttons (Approve / Edit / Reject). The same body
    # is reused as a plain-text fallback when the interactive send path
    # declines, so it must read sensibly without the buttons attached.
    "onboarding.invoice.confirm": (
        "{{ summary }}\n\n"
        "Confirm to submit, Edit to correct a field (reply with "
        "`edit amount: <new amount>` for example), or Reject to discard."
    ),
    # When the SME taps "Edit" with no field — ask which to change.
    "onboarding.invoice.edit.prompt": (
        "Which field would you like to update? Reply like:\n\n"
        "`edit amount: <new amount>`\n"
        "`edit due: <YYYY-MM-DD>`\n"
        "`edit supplier: <supplier name>`\n"
        "`edit buyer: <buyer name>`\n"
        "`edit invoice no: <number>`\n\n"
        "Current draft:\n{{ summary }}"
    ),
    # When the SME taps "Reject" — confirm the discard, no backend write.
    "onboarding.invoice.rejected": (
        "🗑 Discarded — invoice {{ ref }} not submitted.\n\n"
        "Send another invoice anytime as a PDF or photo."
    ),
    # UAT 2026-06-16 (#4): bulk ZIP CSV preview body.
    "onboarding.invoice.batch.preview": (
        "📋 Extracted {{ count }} invoice(s). Review and reply:\n\n"
        "{{ table }}\n\n"
        "Reply `APPROVE ALL` to submit, "
        "`edit <row>: <field> <new value>` to change a row, "
        "or `remove <row>` to drop one."
    ),
    # Bulk: accompanies the review CSV document (the SME edits + sends back).
    "onboarding.invoice.batch.csv_review": (
        "📊 I've attached your {{ count }} invoice(s) as a file "
        "(total {{ total }}).\n\n"
        "• Tap *Approve all* to submit them as-is.\n"
        "• Tap *Reject all* to discard this batch.\n"
        "• Or open the file, fill/correct any rows (especially blank ones "
        "we couldn't read), keep the labels, and send the file back."
    ),
    # Sent when the SME's reply at batch-preview can't be parsed.
    "onboarding.invoice.batch.help": (
        "{{ hint }}\n\n"
        "Reply `APPROVE ALL` to submit, "
        "`edit <row>: <field> <new value>` to change a row, "
        "or `remove <row>` to drop one."
    ),
    # APPROVE ALL → submit done.
    "onboarding.invoice.batch.submitted": (
        "✅ All {{ count }} submitted. Updates incoming at each "
        "stage — disbursement, repayment, closure.\n\n"
        "Send more anytime."
    ),
    # When every row was removed before APPROVE ALL.
    "onboarding.invoice.batch.cleared": (
        "🧹 Batch cleared — nothing submitted. Send another batch "
        "whenever you're ready."
    ),
    # Backend ``transaction.disbursed`` event. Payload per Madad PR #187
    # (UAT 2026-06-16): invoiceNumber, disbursedAmount, utr, dueDate.
    # UAT 2026-06-19 QA #7: include lender + available limit so the
    # SME sees who disbursed + how much credit they have left.
    "onboarding.disbursement.received": (
        "💸 Disbursed!\n\n"
        "📄 Invoice: {{ ref }}\n"
        "💰 Amount: {{ amount }}\n"
        "🏦 Lender: {{ lender }}\n"
        "📅 Due: {{ due_date }}\n"
        "UTR: {{ utr }}\n\n"
        "🔓 Available limit: {{ available_limit }}\n\n"
        "The amount should appear in your bank account shortly. 🏦"
    ),
    # Backend ``repayment.received`` with ``closed=false``. Payload per
    # Madad PR #187: invoiceNumber, amount, totalRepaid, outstandingAmount,
    # emisTotal, emisPaid, emisRemaining, paymasterName, lenderName,
    # availableLimit, currency, dueDate.
    "onboarding.repayment.received": (
        "✅ Repayment received for invoice {{ ref }}.\n\n"
        "This payment: {{ amount }}\n"
        "Total repaid: {{ total_repaid }}\n"
        "Outstanding: {{ outstanding }}\n"
        "EMIs paid: {{ emis_paid }} / {{ emis_total }} "
        "({{ emis_remaining }} remaining)\n"
        "Next due: {{ due_date }}\n\n"
        "Lender: {{ lender }} · Paymaster: {{ paymaster }}\n\n"
        "Thanks for staying on top of it! 🙌"
    ),
    # Kept for back-compat; the unified handler now routes "partially_paid"
    # to ``onboarding.repayment.received`` so this body is rarely seen.
    "onboarding.repayment.partially_paid": (
        "📩 Partial repayment received\n\n"
        "{{ amount }} received for invoice {{ ref }} — "
        "{{ outstanding }} still outstanding.\n\n"
        "Reply here if you have any questions about the remaining balance."
    ),
    # Backend ``repayment.received`` with ``closed=true`` (or the discrete
    # ``repayment.closed`` event — both supported). Only here can we say
    # "fully closed". Show the updated available limit.
    "onboarding.repayment.closed": (
        "🎉 This invoice is now fully closed.\n\n"
        "Invoice: {{ ref }}\n"
        "Total settled: {{ total_repaid }}\n"
        "EMIs paid: {{ emis_paid }} / {{ emis_total }}\n\n"
        "Updated Limit: {{ available_limit }} available.\n\n"
        "Send another invoice anytime — single file or a ZIP both work. 💼"
    ),
    # Single-invoice due/overdue card (in-window free-text mirror of the 5-var
    # Meta template). ━ dividers are static; fields = ref/paymaster/amount/
    # due_date/days. One invoice per message (consolidated multi can't be a
    # Meta template — Meta rejects \n in body vars).
    # Exact PDF wording (single invoice → {{ total }} == {{ amount }}).
    "onboarding.repayment.due_soon": (
        "⏰ Upcoming Payment Reminder\n\n"
        "The following invoices are due within 7 days. Please ensure your "
        "Paymasters are aware:\n"
        "━━━━━━━━━━━━━\n"
        "📄 {{ ref }} · {{ paymaster }}\n"
        "💰 QAR {{ amount }} · Due {{ due_date }} ({{ days }})\n"
        "━━━━━━━━━━━━━\n"
        "🔔 Total due: QAR {{ total }}\n"
        "Need help following up? Reply here or call +974 3017 3888."
    ),
    "onboarding.repayment.overdue": (
        "⚠️ Payment Overdue\n\n"
        "The following invoices are overdue. Please ensure your "
        "Paymasters are aware:\n"
        "━━━━━━━━━━━━━\n"
        "📄 {{ ref }} · {{ paymaster }}\n"
        "💰 QAR {{ amount }} · Due {{ due_date }} ({{ days }})\n"
        "━━━━━━━━━━━━━\n"
        "🔔 Total overdue: QAR {{ total }}\n"
        "Need help following up? Reply here or call +974 3017 3888."
    ),
}


# -- Nudge template bodies ----------------------------------------------------
# Mirror the PDF's three named nudge cadences (Session Lapsed) — each is a
# 3-step sequence at Day N / Day M / Day K. Bodies are short, channel-agnostic
# (WhatsApp + email use the same body); operators can override per-step
# via the CMS admin endpoint after seeding.
_NUDGE_TEMPLATE_BODIES = {
    # Nudge — Financial Report Not Sent
    "nudge.financials_pending.1": (
        "Hi! Just one more document — please share your Audited Financial "
        "Statement when ready. 🙌"
    ),
    "nudge.financials_pending.2": (
        "Need help? Our team can guide you through the next step — "
        "call us on +974 3017 3888 or reply here. 📞"
    ),
    "nudge.financials_pending.3": (
        "Final reminder: your Madad application will be marked inactive if "
        "we don't receive your Audited Financial Statement soon. Reply here "
        "or visit portal.madadfintech.com to continue."
    ),
    # Nudge — Partial Documents
    "nudge.incomplete_docs.1": (
        "Hi! You're almost there. 🚀\n\n"
        "Still needed: {{ documents }}.\n\n"
        "Share here or at portal.madadfintech.com to keep moving."
    ),
    "nudge.incomplete_docs.2": (
        "Quick reminder — we still need: {{ documents }}.\n\n"
        "Reply here with the documents attached, or upload via "
        "portal.madadfintech.com. Need help? Call +974 3017 3888."
    ),
    "nudge.incomplete_docs.3": (
        "Final reminder — your application is at risk of being marked "
        "inactive. Missing: {{ documents }}. Please complete soon at "
        "portal.madadfintech.com or by replying here."
    ),
    # Nudge — Payment Not Received  (link re-sent every step per PDF spec)
    "nudge.payment_pending.1": (
        "Hi! Your application is ready. Complete the onboarding fee of "
        "QAR {{ amount }} to get your application forwarded to banks "
        "today.\n\n"
        "Pay QAR {{ amount }} →\n"
        "{{ payment_link }}"
    ),
    "nudge.payment_pending.2": (
        "Your application slot is reserved. Complete payment before it "
        "expires.\n\n"
        "Pay QAR {{ amount }} →\n"
        "{{ payment_link }}\n\n"
        "Questions? Reply here or call +974 3017 3888."
    ),
    "nudge.payment_pending.3": (
        "Final reminder — your slot will be released if payment is not "
        "received soon. Payment link re-sent below.\n\n"
        "Pay QAR {{ amount }} →\n"
        "{{ payment_link }}"
    ),
}

# -- Nudge schedules (PDF §"NUDGE" tables, Steps 2 / 4 / 5) -----------------
# Each schedule = ordered list of steps. Each step fires after `offset` seconds
# elapse since the workflow scheduled the reminder. `channels` is the channel
# bag to deliver on (per-channel CMS template bodies share the same key in
# this seed; operators may diverge later).
_NUDGE_SCHEDULES: dict[str, dict[str, Any]] = {
    # 1. Nudge — Financial Report Not Sent (PDF §2): WA Day 2 / WA+Email Day 5
    # / Email Day 14
    "financials_pending": {
        "schedule": [
            {
                "offset": DAY_2,
                "channels": ["whatsapp"],
                "template_key": "nudge.financials_pending.1",
            },
            {
                "offset": DAY_5,
                "channels": ["whatsapp", "email"],
                "template_key": "nudge.financials_pending.2",
            },
            {
                "offset": DAY_14,
                "channels": ["email"],
                "template_key": "nudge.financials_pending.3",
                "escalate": True,
            },
        ],
        "max_attempts": 3,
    },
    # 2. Nudge — Partial Documents (PDF §4): WA Day 3 / WA+Email Day 7 /
    # Email Day 14
    "incomplete_docs": {
        "schedule": [
            {
                "offset": DAY_3,
                "channels": ["whatsapp"],
                "template_key": "nudge.incomplete_docs.1",
            },
            {
                "offset": DAY_7,
                "channels": ["whatsapp", "email"],
                "template_key": "nudge.incomplete_docs.2",
            },
            {
                "offset": DAY_14,
                "channels": ["email"],
                "template_key": "nudge.incomplete_docs.3",
                "escalate": True,
            },
        ],
        "max_attempts": 3,
    },
    # 3. Nudge — Payment Not Received (PDF §5): WA Day 1 / WA+Email Day 3 /
    # Email Day 7; PDF mandates payment link re-sent each step — every step
    # template includes {{ payment_link }} so the workflow's per-nudge
    # variables resolve the live link at dispatch time.
    "payment_pending": {
        "schedule": [
            {
                "offset": DAY_1,
                "channels": ["whatsapp"],
                "template_key": "nudge.payment_pending.1",
            },
            {
                "offset": DAY_3,
                "channels": ["whatsapp", "email"],
                "template_key": "nudge.payment_pending.2",
            },
            {
                "offset": DAY_7,
                "channels": ["email"],
                "template_key": "nudge.payment_pending.3",
                "escalate": True,
            },
        ],
        "max_attempts": 3,
    },
    # Trailing-edge "any more documents?" prompt (UAT 2026-06-13). A multi-file
    # WhatsApp upload arrives as many SEPARATE inbound waves; there is no
    # in-workflow "uploads finished" signal. The documents loop (re)arms this
    # single short nudge on every upload wave and suppresses it on the next, so
    # the prompt fires ONCE — only after the SME has been quiet for ~the delay
    # below — never mid-batch. The worker tick is 60s, so effective quiet window
    # is ≈ 40–100s.
    "docs_more_prompt": {
        "schedule": [
            {
                "offset": 25,
                "channels": ["whatsapp"],
                "template_key": "onboarding.documents.settle_prompt",
            },
        ],
        "max_attempts": 1,
    },
}


    # Email subjects (UAT 2026-06-28 — Ishan #A). Only the steps the SME
    # actually sees on email get explicit subjects; the rest fall back to
    # ``_DEFAULT_EMAIL_SUBJECT`` ("Madad Financing — update on your
    # application") at the gateway. Wording mirrors the email subjects in
    # the agentic-flow PDF so the SME's inbox reads like the canonical
    # journey rather than a bag of generic notifications.
_EMAIL_SUBJECTS: dict[str, str] = {
    "onboarding.campaign.intro":         "Madad — Unlock Cash Stuck in Your Invoices",
    "onboarding.cr.received":            "Madad — CR Received · One More Document Needed",
    "onboarding.financials.received":    "Madad — Financial Report Received · Pre-Qualification in 24 Hours",
    "onboarding.documents.checklist":    "Madad — Pre-Qualified! Please Share Your Documents",
    "onboarding.documents.single_received": "Madad — Documents Received · Checklist Update",
    "onboarding.documents.complete":     "Madad — All Documents Received",
    "onboarding.payment.request":        "Madad — Your Application Result & Next Step",
    "onboarding.payment.confirmed":      "Madad — Payment Received · Application Forwarded to Banks",
    "onboarding.offers.preview":         "Madad — Your Financing Offers Are Ready · Login to Select",
    "onboarding.offer.confirmed":        "Madad — Offer Selection Confirmed",
    "onboarding.activated":              "Madad — Your Credit Line is Active",
    "onboarding.invoice.received":       "Madad — Invoice Received",
    "onboarding.invoice.submitted":      "Madad — Invoice Submitted for Financing",
    "onboarding.invoice.batch.preview":  "Madad — Invoice Review File Attached",
    "onboarding.disbursement.received":  "Madad — Invoice Disbursed",
    "onboarding.repayment.received":     "Madad — Repayment Received",
    "onboarding.repayment.closed":       "Madad — Invoice Closed",
    "onboarding.repayment.due_soon":     "Madad — Invoices Due Soon",
    "onboarding.repayment.overdue":      "Madad — Invoice Overdue",
    "onboarding.welcome_back":           "Madad — Welcome Back",
    "onboarding.not_eligible":           "Madad — Eligibility Check Result",
    "onboarding.not_pre_qualified":      "Madad — Pre-Qualification Update",
    "onboarding.not_qualified":          "Madad — Application Update",
    "onboarding.domain_blocked":         "Madad — Application Update",
}


async def run() -> int:
    cms = get_cms_service()

    # -- onboarding templates --
    seeded_templates = 0
    skipped: list[str] = []
    for key in TEMPLATE_KEYS:
        body = _TEMPLATE_BODIES.get(key)
        if body is None:
            skipped.append(key)
            continue
        await cms.upsert_template(
            key, Locale.EN, body,
            subject=_EMAIL_SUBJECTS.get(key),
        )
        seeded_templates += 1
        print(f"  ✓ template: {key}")

    # -- nudge step templates --
    seeded_nudge_templates = 0
    for key, body in _NUDGE_TEMPLATE_BODIES.items():
        await cms.upsert_template(key, Locale.EN, body)
        seeded_nudge_templates += 1
        print(f"  ✓ nudge template: {key}")

    # -- nudge schedules (ConfigKind.NUDGE keyed on reason string) --
    seeded_schedules = 0
    for reason, schedule_value in _NUDGE_SCHEDULES.items():
        await cms.upsert(ConfigKind.NUDGE, reason, schedule_value)
        seeded_schedules += 1
        print(f"  ✓ nudge schedule: {reason}")

    # -- document checklists (ConfigKind.CHECKLIST) --
    # Vendor Plan M1 acceptance criterion: adding a new required document via
    # the CMS reflects in the agent's next conversation within 5 minutes.
    # Seeding the WhatsApp default lets ops edit via
    # ``POST /cms/checklists/onboarding.whatsapp.required_docs``.
    seeded_checklists = 0
    whatsapp_items = [
        ChecklistItem(
            code=code,
            label={"en": DOCUMENT_LABELS.get(code, code)},
            required=True,
        )
        for code in DEFAULT_WHATSAPP_REQUIRED_DOCS
    ]
    await cms.upsert_checklist("onboarding.whatsapp.required_docs", whatsapp_items)
    seeded_checklists += 1
    print("  ✓ checklist: onboarding.whatsapp.required_docs")

    if skipped:
        print(f"\n  Skipped (no body): {skipped}")
    print(
        f"\n  Seeded {seeded_templates} onboarding templates, "
        f"{seeded_nudge_templates} nudge step templates, "
        f"{seeded_schedules} nudge schedules, and "
        f"{seeded_checklists} document checklists into the CMS."
    )
    return 0


def main() -> int:
    return asyncio.run(run())


if __name__ == "__main__":
    raise SystemExit(main())
