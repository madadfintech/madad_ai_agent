"""Wiring for the onboarding workflow runtime."""

from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache

from app.shared.workflow import WorkflowRuntime, build_runtime

from .dispatcher import OnboardingDispatcher
from .onboarding import OnboardingWorkflow
from .ports import (
    DocumentIntake,
    InMemoryDocumentIntake,
    InMemoryMadadClient,
    InMemoryPaymentClient,
    MadadClient,
    Messenger,
    PaymentClient,
    RecordingMessenger,
    RecordingReminders,
    Reminders,
)


@dataclass
class OnboardingPlatform:
    """Everything needed to drive onboarding: runtime, workflow, dispatcher."""

    runtime: WorkflowRuntime
    workflow: OnboardingWorkflow
    dispatcher: OnboardingDispatcher


# Default required onboarding documents (the dynamic checklist lives in CMS; this
# seeds the in-memory intake used when no CMS-backed intake is wired).
DEFAULT_REQUIRED_DOCS = ["trade_license", "tax_card", "bank_statement"]
DEFAULT_DOC_KEYWORDS = {
    "trade": "trade_license",
    "tax": "tax_card",
    "bank": "bank_statement",
}


def build_onboarding_platform(
    *,
    messenger: Messenger | None = None,
    documents: DocumentIntake | None = None,
    madad: MadadClient | None = None,
    payments: PaymentClient | None = None,
    reminders: Reminders | None = None,
    runtime: WorkflowRuntime | None = None,
) -> OnboardingPlatform:
    runtime = runtime or build_runtime()
    workflow = OnboardingWorkflow(
        messenger=messenger or RecordingMessenger(),
        documents=documents
        or InMemoryDocumentIntake(
            required=DEFAULT_REQUIRED_DOCS, type_by_keyword=DEFAULT_DOC_KEYWORDS
        ),
        madad=madad or InMemoryMadadClient(),
        payments=payments or InMemoryPaymentClient(),
        reminders=reminders or RecordingReminders(),
    )
    runtime.register(workflow)
    dispatcher = OnboardingDispatcher(runtime)
    return OnboardingPlatform(runtime=runtime, workflow=workflow, dispatcher=dispatcher)


@lru_cache(maxsize=1)
def get_onboarding_platform() -> OnboardingPlatform:
    """Process-singleton platform for the FastAPI app.

    Uses in-memory adapters for the onboarding business ports (MadadClient,
    PaymentClient, DocumentIntake). The MCP-backed replacements for these are
    introduced in later integration phases (channel-session, KYC, monetization
    payment) — each will plug into ``build_onboarding_platform(...)`` once its
    adapter exists. ``settings.mcp.enabled`` switches the *transport* (real
    fastmcp client vs in-memory fake); the workflow-level adapters select
    themselves once they ship.
    """

    return build_onboarding_platform()
