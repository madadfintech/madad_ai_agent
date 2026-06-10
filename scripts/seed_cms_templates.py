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
from app.services.workflow import TEMPLATE_KEYS
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
        "Please reply YES or NO. For any query call 72773652."
    ),
    "onboarding.help.what_is_madad": (
        "Hello! 👋\n\n"
        "We are Madad (madadfintech.com) — a regulated business finance company "
        "in Qatar. We help businesses unlock working capital from invoices owed "
        "by enterprise or government clients.\n\n"
        "✅ Fast financing — funds within 5 working days of approval\n"
        "✅ Multiple bank offers — you choose\n"
        "✅ Sharia-compliant · Regulated by Qatar Central Bank\n\n"
        "You can verify us at madadfintech.com or call 72773652."
    ),
    "onboarding.help.security": (
        "Absolutely — your data is completely safe with us. 🔒\n\n"
        "We are a regulated entity under Qatar Central Bank. The consent simply "
        "means you agree that Madad may access your business information to "
        "assess eligibility and use the document for financing purposes. That's all.\n\n"
        "You can verify us at madadfintech.com or call 72773652."
    ),
    "onboarding.help.contextual": (
        "{{ answer }}\n\n"
        "{{ next_step }}\n\n"
        "For any query call 72773652."
    ),
    "onboarding.declined": (
        "No problem at all! If you ever need working capital support in the future, "
        "we're here. Reach us at madadfintech.com or call 72773652. Have a great day! 👋"
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
        "Your Madad account is all set! 🎉\n\n"
        "What's your business email? We'll use it for your account and to keep "
        "you updated on your application. 📧"
    ),
    "onboarding.business_email.conflict": (
        "Looks like a business is already registered with that email.\n\n"
        "Please reply with a different business email, or contact our support "
        "team at support@madadfintech.com and we'll help you out. 📧"
    ),
    "onboarding.consent.request": (
        "Great to know! 🎉 We have financed many businesses like yours in Qatar.\n\n"
        "To start the journey we need to first verify that your business is in Qatar "
        "and eligible for financing.\n\n"
        "We need your Commercial Registration (CR) to verify this.\n\n"
        "Before you share, please note:\n"
        "✅ We are a regulated entity under Qatar Central Bank\n"
        "🔗 Privacy Policy: https://www.madadfintech.com/en/privacy-policy\n"
        "🔒 Terms & Conditions: https://www.madadfintech.com/en/terms-and-conditions\n\n"
        "By sharing your CR you agree to the above. Please go ahead and share "
        "your CR document as a PDF or photo in this chat.\n\n"
        "Any questions? Reply here or call us on 72773652."
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
    "onboarding.financials.request": (
        "Awesome, thanks for sharing! 🙌\n\n"
        "We can see that your business is registered in Qatar — all good so far! ✅\n\n"
        "To further assess your eligibility we need to know your financials. "
        "Please share your last Audited Financial Statement.\n\n"
        "For any query call us on 72773652."
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
        "#{{ ref }}. You can login at madadfintech.com and track your status anytime."
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
        "📤 Share the documents here or login at madadfintech.com to complete "
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
        "We will share your assessment report within 24 hours. If all looks good, "
        "we will forward your application to our banking partners in Qatar.\n\n"
        "Meanwhile, enjoy your coffee ☕\n\n"
        "For any query call 72773652 or visit madadfintech.com"
    ),
    "onboarding.upload.required": (
        "Whenever you're ready, please share {{ document }} as a PDF or photo here "
        "and I'll take it from there. 🙂\n\n"
        "Have a question? Just ask — happy to help. For any query call 72773652."
    ),
    # Per user (UAT 2026-06-10): after the coffee message we explicitly ask
    # the SME whether they have any more documents to send (classifier
    # failures + the "I forgot one" case). Reply YES / NO; existing
    # synonym set covers Ok / Sure / Nope / etc. WhatsApp interactive
    # reply-button send is on the cluster's backlog; until that ships,
    # this is plain-text + the synonym-aware matcher.
    "onboarding.documents.more_docs_prompt": (
        "📄 Do you have any more documents to upload?\n\n"
        "Reply YES if you'd like to send more, or NO if you're done — "
        "we'll proceed with the next step."
    ),
    # Immediate ack the instant a valid CR attachment arrives — guarantees the
    # user always sees a response even if the downstream upload + financials
    # prompt fails (QA Bug #1 + Ishan handover §9 / 2026-06-09).
    "onboarding.cr.received": (
        "📄 Got your CR — processing it now…"
    ),
    # Immediate ack on any document upload in the post-prequal docs loop.
    # Rewritten 2026-06-09 (UAT feedback): the previous copy assumed the
    # SME sent a ZIP and was wordy / unprofessional. Now generic, short,
    # and channel-agnostic so it fits both single-file and ZIP uploads.
    "onboarding.documents.processing": (
        "📄 Got it — validating your document(s) now…"
    ),
    # Final fallback sent at the end of the docs loop when neither the
    # classifier nor the local-unzip pipeline could land a single file the
    # backend accepted. Keeps the SME informed instead of dropping silent.
    "onboarding.documents.upload_failed": (
        "I received your file(s) but couldn't process them right now. "
        "Please resend any failed documents as separate PDF/photo uploads, "
        "or call 72773652 if it keeps happening."
    ),
    "onboarding.status.pending": (
        "Hi! Your application is currently under review with Madad. 👍\n\n"
        "I'll notify you as soon as the next update is available. You can also "
        "track your status at madadfintech.com. For queries call 72773652."
    ),
    "onboarding.payment.awaiting": (
        "Your application is ready to move forward. Please complete the secure "
        "QAR 6,000 onboarding and assessment fee payment using the link shared above.\n\n"
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
        "Track your status at madadfintech.com (Ref: {{ ref }})"
    ),
    "onboarding.not_qualified": (
        "Unfortunately your application wasn't accepted by our lender "
        "partners this time. Please reach out to our team."
    ),
    # Body shown ABOVE the "Pay QAR … →" CTA button (no raw link — the button
    # carries it). Used for the interactive WhatsApp send.
    "onboarding.payment.request.button": (
        "Hello! 👋\n\n"
        "Your application has been reviewed by our team. Here is your result:\n\n"
        "📊 Madad Score: {{ score }}/100 · Strong\n\n"
        "Based on this score, we believe you have high chances of getting approval "
        "from our banking partners. 💪\n\n"
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
        "📊 Madad Score: {{ score }}/100 · Strong\n\n"
        "Based on this score, we believe you have high chances of getting approval "
        "from our banking partners. 💪\n\n"
        "Your application is ready to be forwarded.\n"
        "To submit your application to the banks, a one-time onboarding and assessment "
        "fee of QAR {{ amount }} is required.\n\n"
        "Pay QAR {{ amount }} →\n"
        "{{ payment_link }}\n\n"
        "Once payment is received, your application will be forwarded immediately."
    ),
    "onboarding.offers.preview": (
        "🎉 Exciting news — your financing offers are ready!\n\n"
        "{{ offer_cards }}\n\n"
        "💬 Feel free to ask me anything about these offers right here!"
    ),
    "onboarding.offer.handoff": (
        "💬 Feel free to ask me anything about these offers right here!\n\n"
        "When you're ready to select, please login to your Madad account to finalise "
        "your offer — this is where you'll also manage your invoices going forward.\n\n"
        "Login to Madad Platform → madadfintech.com"
    ),
    # Spec Step 8 button variant — body for the WhatsApp interactive CTA-URL
    # message. The button label ("Login to Madad →") is supplied at send-time
    # (capped at 20 chars by Meta); this body is what shows above the button.
    "onboarding.offer.handoff.button": (
        "💬 Feel free to ask me anything about these offers right here!\n\n"
        "When you're ready to select, please login to your Madad account to "
        "finalise your offer — this is where you'll also manage your invoices "
        "going forward."
    ),
    # PDF Step 9 — credit line activated, surfaces the accepted offer details
    # (bank, limit, rate, tenure) inline so the SME has the key numbers in
    # hand without opening the platform.
    "onboarding.activated": (
        "🎊 Your financing line is ACTIVE!\n\n"
        "🏦 {{ lender }} · 💰 {{ limit }} · 📈 {{ rate }} · ⏱ {{ tenure }}\n\n"
        "You can now submit invoices for financing right here — send a single "
        "PDF or multiple invoices at once. 📄\n\n"
        "Track at madadfintech.com (Ref: {{ ref }})"
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
        "call us on 72773652 or reply here. 📞"
    ),
    "nudge.financials_pending.3": (
        "Final reminder: your Madad application will be marked inactive if "
        "we don't receive your Audited Financial Statement soon. Reply here "
        "or visit madadfintech.com to continue."
    ),
    # Nudge — Partial Documents
    "nudge.incomplete_docs.1": (
        "Hi! You're almost there. 🚀\n\n"
        "Still needed: {{ documents }}.\n\n"
        "Share here or at madadfintech.com to keep moving."
    ),
    "nudge.incomplete_docs.2": (
        "Quick reminder — we still need: {{ documents }}.\n\n"
        "Reply here with the documents attached, or upload via "
        "madadfintech.com. Need help? Call 72773652."
    ),
    "nudge.incomplete_docs.3": (
        "Final reminder — your application is at risk of being marked "
        "inactive. Missing: {{ documents }}. Please complete soon at "
        "madadfintech.com or by replying here."
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
        "Questions? Reply here or call 72773652."
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
        await cms.upsert_template(key, Locale.EN, body)
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

    if skipped:
        print(f"\n  Skipped (no body): {skipped}")
    print(
        f"\n  Seeded {seeded_templates} onboarding templates, "
        f"{seeded_nudge_templates} nudge step templates, and "
        f"{seeded_schedules} nudge schedules into the CMS."
    )
    return 0


def main() -> int:
    return asyncio.run(run())


if __name__ == "__main__":
    raise SystemExit(main())
