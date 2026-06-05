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
        "Welcome to MADAD! Reply YES to start your invoice financing setup, "
        "or NO to opt out."
    ),
    "onboarding.declined": (
        "Thanks for your time. Reply START anytime to begin onboarding."
    ),
    "onboarding.domain_blocked": (
        "It looks like {{ domain }} is already registered with another team. "
        "Please contact your administrator or use a personal email."
    ),
    "onboarding.collect_details.request": (
        "Welcome to MADAD! To create your account please share: "
        "1) Your first + last name "
        "2) Your business's legal entity name "
        "3) Your CR (Commercial Registration) number "
        "4) Is your business based in Qatar? "
        "5) Your role at the business (FOUNDER / DIRECTOR / SHAREHOLDER / ...)"
    ),
    "onboarding.consent.request": (
        "We need your consent + Commercial Registration document. Reply with "
        "the CR as an attachment to continue."
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
        "Please share your audited financial report (PDF attachment)."
    ),
    "onboarding.buyers.request": (
        "Please share your main buyer's details (name, country, contact)."
    ),
    "onboarding.shareholders.request": (
        "Please share your shareholders' details (name, percentage)."
    ),
    "onboarding.documents.checklist": (
        "We need the following documents to proceed: {{ documents }}. "
        "Please reply with them as attachments."
    ),
    "onboarding.documents.missing": (
        "We're still missing: {{ documents }}. Please reply with the remaining "
        "documents."
    ),
    "onboarding.documents.complete": (
        "All required documents received — thank you!"
    ),
    "onboarding.not_qualified": (
        "Unfortunately your application wasn't accepted by our lender "
        "partners this time. Please reach out to our team."
    ),
    "onboarding.payment.request": (
        "Your onboarding fee of QAR {{ amount }} is ready. Please complete "
        "payment via the secure link below:\n\n"
        "{{ payment_link }}\n\n"
        "Reference: {{ provider_ref }}"
    ),
    "onboarding.offers.preview": (
        "Great news — you have {{ count }} financing offer(s) ready to review!"
    ),
    "onboarding.offer.handoff": (
        "Please visit madadfintech.com to review and accept your offer. "
        "You'll receive a confirmation once you're done."
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
