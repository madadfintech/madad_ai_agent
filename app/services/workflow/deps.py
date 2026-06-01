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

    With ``mcp.enabled`` the full production chain is wired: outbound through the
    Communication service, reminders through Nudge, and Madad/Tess/document
    routing through the MCP client. Otherwise the in-memory fakes are used
    (dev/tests), so the default behaviour is unchanged.
    """

    from app.core.config import settings

    if not settings.mcp.enabled:
        return build_onboarding_platform()

    from app.services.communication.deps import get_communication_service
    from app.services.nudge.deps import get_nudge_service
    from app.shared.mcp import get_mcp_client

    from .adapters import CommunicationMessenger, NudgeReminders
    from .mcp_adapters import McpDocumentIntake, McpMadadClient, McpPaymentClient

    client = get_mcp_client()
    return build_onboarding_platform(
        messenger=CommunicationMessenger(get_communication_service()),
        reminders=NudgeReminders(get_nudge_service()),
        documents=McpDocumentIntake(client),
        madad=McpMadadClient(client),
        payments=McpPaymentClient(client),
    )
