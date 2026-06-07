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

from app.services.cms.deps import get_cms_service
from app.services.workflow import TEMPLATE_KEYS
from app.shared.i18n import Locale

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
        "You can have cash in your account within 5 working days of completing your application!\n\n"
        "We now need the following documents to complete your application:\n"
        "{{ documents }}\n\n"
        "📤 Share the documents here as individual PDFs/photos or as a ZIP file."
    ),
    "onboarding.documents.missing": (
        "✅ Got it — {{ received }} of {{ total }} documents received! 🙌\n\n"
        "⏳ Still needed:\n"
        "{{ documents }}\n\n"
        "No rush — send them one at a time or all together."
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
    "onboarding.not_qualified": (
        "Unfortunately your application wasn't accepted by our lender "
        "partners this time. Please reach out to our team."
    ),
    "onboarding.payment.request": (
        "Hello! 👋\n\n"
        "Your application has been reviewed by our team. Based on this score, "
        "we believe you have high chances of getting approval from our banking partners. 💪\n\n"
        "Your application is ready to be forwarded.\n"
        "To submit your application to the banks, a one-time onboarding and assessment fee "
        "of QAR {{ amount }} is required.\n\n"
        "Pay QAR {{ amount }} →\n"
        "{{ payment_link }}\n\n"
        "To know more about our pricing, visit madadfintech.com/pricing.\n\n"
        "Once payment is received, your application will be forwarded immediately.\n\n"
        "Reference: {{ provider_ref }}"
    ),
    "onboarding.offers.preview": (
        "🎉 Exciting news — your financing offers are ready!\n\n"
        "You have {{ count }} financing offer(s) ready to review."
    ),
    "onboarding.offer.handoff": (
        "💬 Feel free to ask me anything about these offers right here!\n\n"
        "When you're ready to select, please login to your Madad account to finalise "
        "your offer — this is where you'll also manage your invoices going forward.\n\n"
        "Login to Madad Platform → madadfintech.com"
    ),
    "onboarding.activated": (
        "Your credit line is now active! You can start submitting invoices "
        "for financing."
    ),
}


async def run() -> int:
    cms = get_cms_service()
    seeded = 0
    skipped: list[str] = []
    for key in TEMPLATE_KEYS:
        body = _TEMPLATE_BODIES.get(key)
        if body is None:
            skipped.append(key)
            continue
        await cms.upsert_template(key, Locale.EN, body)
        seeded += 1
        print(f"  ✓ {key}")
    if skipped:
        print(f"\n  Skipped (no body): {skipped}")
    print(f"\n  Seeded {seeded} templates into the CMS.")
    return 0


def main() -> int:
    return asyncio.run(run())


if __name__ == "__main__":
    raise SystemExit(main())
