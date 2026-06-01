"""Fixtures for onboarding workflow tests (in-memory ports + runtime)."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

import pytest

from app.services.workflow import (
    InMemoryDocumentIntake,
    InMemoryMadadClient,
    InMemoryPaymentClient,
    OnboardingPlatform,
    RecordingMessenger,
    RecordingReminders,
    build_onboarding_platform,
)

REQUIRED_DOCS = ["trade_license", "tax_card"]
DOC_KEYWORDS = {"trade": "trade_license", "tax": "tax_card", "audited": "audited_report"}


@dataclass
class Harness:
    platform: OnboardingPlatform
    messenger: RecordingMessenger
    documents: InMemoryDocumentIntake
    madad: InMemoryMadadClient
    payments: InMemoryPaymentClient
    reminders: RecordingReminders


@pytest.fixture
def make_harness() -> Callable[..., Harness]:
    def _make(*, eligible: bool = True, required: list[str] | None = None) -> Harness:
        messenger = RecordingMessenger()
        documents = InMemoryDocumentIntake(
            required=required or REQUIRED_DOCS, type_by_keyword=DOC_KEYWORDS
        )
        madad = InMemoryMadadClient(eligible=eligible)
        payments = InMemoryPaymentClient()
        reminders = RecordingReminders()
        platform = build_onboarding_platform(
            messenger=messenger,
            documents=documents,
            madad=madad,
            payments=payments,
            reminders=reminders,
        )
        return Harness(platform, messenger, documents, madad, payments, reminders)

    return _make


@pytest.fixture
def harness(make_harness: Callable[..., Harness]) -> Harness:
    return make_harness()
