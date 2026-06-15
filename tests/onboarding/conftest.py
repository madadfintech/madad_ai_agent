"""Fixtures for onboarding workflow tests (in-memory ports + runtime)."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

import pytest

from app.services.workflow import (
    InMemoryInvoiceClient,
    InMemoryKycClient,
    InMemoryMadadIdentityClient,
    InMemoryMonetizationPaymentClient,
    OnboardingPlatform,
    RecordingMessenger,
    RecordingReminders,
    build_onboarding_platform,
)

REQUIRED_DOCS = ["trade_license", "tax_card"]


@dataclass
class Harness:
    platform: OnboardingPlatform
    messenger: RecordingMessenger
    identity: InMemoryMadadIdentityClient
    kyc: InMemoryKycClient
    payments: InMemoryMonetizationPaymentClient
    reminders: RecordingReminders
    invoices: InMemoryInvoiceClient


@pytest.fixture
def make_harness() -> Callable[..., Harness]:
    def _make(
        *,
        known_phones: dict[str, str] | None = None,
        known_emails: dict[str, str] | None = None,
        blocked_domains: dict[str, str] | None = None,
        journey_status: str = "ELIGIBLE",
        required_docs: list[str] | None = None,
        eligibility_result: dict[str, object] | None = None,
        business_details: dict[str, object] | None = None,
        products: list[dict[str, object]] | None = None,
        seeded_invoices: list[dict[str, object]] | None = None,
    ) -> Harness:
        messenger = RecordingMessenger()
        identity = InMemoryMadadIdentityClient(
            known_phones=known_phones,
            known_emails=known_emails,
            blocked_domains=blocked_domains,
            journey_status=journey_status,
        )
        kyc = InMemoryKycClient(
            required_documents=required_docs or REQUIRED_DOCS,
            eligibility_result=eligibility_result,
        )
        payments = InMemoryMonetizationPaymentClient(
            business_details=business_details,
            products=products,
        )
        reminders = RecordingReminders()
        invoices = InMemoryInvoiceClient(invoices=seeded_invoices)
        platform = build_onboarding_platform(
            messenger=messenger,
            identity=identity,
            kyc=kyc,
            payments=payments,
            reminders=reminders,
            invoices=invoices,
        )
        return Harness(
            platform, messenger, identity, kyc, payments, reminders, invoices,
        )

    return _make


@pytest.fixture
def harness(make_harness: Callable[..., Harness]) -> Harness:
    return make_harness()
