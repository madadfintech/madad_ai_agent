"""Vendor Plan M1 acceptance: dynamic document checklist via CMS.

The plan requires that adding a new required document to the backend config
reflect in the agent's next conversation. The workflow's WhatsApp path now
consumes ``ChecklistProvider`` first and falls back to
``DEFAULT_WHATSAPP_REQUIRED_DOCS`` only when the provider is unset or empty.
These tests pin that contract.
"""

from __future__ import annotations

import logging

from app.services.document.checklist import InMemoryChecklistProvider
from app.services.workflow.deps import build_onboarding_platform
from app.services.workflow.onboarding import DEFAULT_WHATSAPP_REQUIRED_DOCS
from app.services.workflow.state import OnboardingState
from app.shared.workflow import Channel
from app.shared.workflow.context import SystemClock, WorkflowContext
from app.shared.workflow.events import InMemoryEventBus

WA = Channel.WHATSAPP
IDENTITY = "+97455500091"


def _ctx() -> WorkflowContext:
    return WorkflowContext(
        run_id="r1",
        session_id="s1",
        thread_id="t1",
        workflow="onboarding",
        version=1,
        channel=WA,
        identity=IDENTITY,
        clock=SystemClock(),
        events=InMemoryEventBus(),
        logger=logging.getLogger("test"),
    )


async def test_documents_list_fetch_uses_cms_checklist_when_provider_present() -> None:
    """A CMS-configured checklist overrides the hardcoded default for WhatsApp."""
    custom_codes = [
        "trade_license",
        "tax_card",
        "audited_report",
        "bank_statement",
        "national_address_certificate",
    ]
    checklist = InMemoryChecklistProvider()
    checklist.add("onboarding.whatsapp.required_docs", custom_codes)
    platform = build_onboarding_platform(checklist=checklist)

    state = OnboardingState(identity=IDENTITY)
    step = await platform.workflow._documents_list_fetch(state, _ctx())  # noqa: SLF001

    assert step["missing_documents"] == custom_codes


async def test_documents_list_fetch_falls_back_to_default_when_cms_empty() -> None:
    """Missing CMS key → fallback to ``DEFAULT_WHATSAPP_REQUIRED_DOCS``.

    Guarantees the agent never ships an empty doc list to the SME when ops
    forget to seed the checklist.
    """
    checklist = InMemoryChecklistProvider()  # no entries
    platform = build_onboarding_platform(checklist=checklist)

    state = OnboardingState(identity=IDENTITY)
    step = await platform.workflow._documents_list_fetch(state, _ctx())  # noqa: SLF001

    assert step["missing_documents"] == list(DEFAULT_WHATSAPP_REQUIRED_DOCS)


async def test_documents_list_fetch_falls_back_when_no_provider() -> None:
    """checklist=None (the default) → fallback path. Pre-existing tests rely
    on this and the harness builds platforms this way by default."""
    platform = build_onboarding_platform()

    state = OnboardingState(identity=IDENTITY)
    step = await platform.workflow._documents_list_fetch(state, _ctx())  # noqa: SLF001

    assert step["missing_documents"] == list(DEFAULT_WHATSAPP_REQUIRED_DOCS)


async def test_documents_list_fetch_caches_resolved_list_on_state() -> None:
    """M1 acceptance — the CMS-resolved required-docs list is cached on
    ``state.required_documents`` so every downstream progress meter
    (``X of N received``, pending-docs summary, LLM off-script context)
    reads from THIS list rather than the Python constant. When ops adds
    an 11th doc via the CMS, the progress meter must read "of 11", not
    "of 10".
    """
    custom_codes = [
        "trade_license",
        "tax_card",
        "audited_report",
        "audited_report_2023",
        "audited_report_2022",
        "credit_bureau_report",
        "payable_ageing",
        "receivable_ageing",
        "interim_financial_statement",
        "bank_statement_6_months",
        "national_address_certificate",  # the 11th — ops added this
    ]
    checklist = InMemoryChecklistProvider()
    checklist.add("onboarding.whatsapp.required_docs", custom_codes)
    platform = build_onboarding_platform(checklist=checklist)

    state = OnboardingState(identity=IDENTITY)
    step = await platform.workflow._documents_list_fetch(state, _ctx())  # noqa: SLF001

    assert step["missing_documents"] == custom_codes
    assert step["required_documents"] == custom_codes
    # Progress meter sourcing: len(state.required_documents) is what
    # _documents_upload_loop_send uses for the "of N" denominator.
    assert len(step["required_documents"]) == 11


async def test_documents_list_fetch_fallback_also_populates_state() -> None:
    """Even when the fallback fires, ``state.required_documents`` must be
    populated — otherwise downstream progress meters degrade to 0/0."""
    checklist = InMemoryChecklistProvider()
    platform = build_onboarding_platform(checklist=checklist)

    state = OnboardingState(identity=IDENTITY)
    step = await platform.workflow._documents_list_fetch(state, _ctx())  # noqa: SLF001

    assert step["missing_documents"] == list(DEFAULT_WHATSAPP_REQUIRED_DOCS)
    assert step["required_documents"] == list(DEFAULT_WHATSAPP_REQUIRED_DOCS)
